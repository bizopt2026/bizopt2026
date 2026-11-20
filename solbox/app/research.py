"""웹 리서치: 주제와 실행 주기 설정."""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.labels import ERROR_KINDS, RESEARCH_AREAS
from app.models import AIJob, AppSetting, ResearchReport, ResearchRun, ResearchTopic, Solution
from app import checks
from app.proposals import check_sources

if TYPE_CHECKING:
    from app.ai.research_tasks import AreaOut, SuggestOut

log = logging.getLogger(__name__)

SCHEDULE_KEY = "research_schedule"
LAST_SLOT_KEY = "research_last_slot"  # 예약으로 시작한 가장 최근 예정 시각 (ISO)


@dataclass
class Schedule:
    enabled: bool = True
    weekday: int = 0
    hour: int = 6
    minute: int = 0


def _int_in(value, low: int, high: int, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        return default
    return value


def get_schedule(db: Session) -> Schedule:
    default = Schedule()
    row = db.get(AppSetting, SCHEDULE_KEY)
    raw = row.value if row is not None and isinstance(row.value, dict) else {}
    enabled = raw.get("enabled", default.enabled)
    return Schedule(
        enabled=enabled if isinstance(enabled, bool) else default.enabled,
        weekday=_int_in(raw.get("weekday"), 0, 6, default.weekday),
        hour=_int_in(raw.get("hour"), 0, 23, default.hour),
        minute=_int_in(raw.get("minute"), 0, 59, default.minute),
    )


def set_schedule(db: Session, s: Schedule) -> None:
    value = asdict(s)
    row = db.get(AppSetting, SCHEDULE_KEY)
    if row is None:
        db.add(AppSetting(key=SCHEDULE_KEY, value=value))
    else:
        row.value = value
    db.commit()


def active_topics(db: Session) -> dict[str, list[ResearchTopic]]:
    """분야별 사용 중인 주제 (주제가 없는 분야는 키가 없다)."""
    topics = db.query(ResearchTopic).filter_by(is_active=True).order_by(ResearchTopic.id).all()
    result: dict[str, list[ResearchTopic]] = {}
    for area in RESEARCH_AREAS:
        items = [t for t in topics if t.area == area]
        if items:
            result[area] = items
    return result


URL_MAX = 2000
MAX_TRENDS, MAX_INSIGHTS, MAX_SUGGESTIONS = 10, 7, 7
NO_SOURCE_NOTE = checks.NO_LINK_NOTE
UNOPENED_NOTE = checks.UNOPENED
UNOPENED_LABEL = "검색 결과 링크 (본문 미확인)"
NO_TOPICS_MESSAGE = "사용 중인 리서치 주제가 없습니다."


_INVISIBLE = {*range(0x200B, 0x2010), *range(0x202A, 0x202F), *range(0x2066, 0x206A), 0xFEFF}


def clean_url(url: str) -> str | None:
    """http(s) 주소만 돌려준다. 공백·제어문자가 있거나 호스트가 없거나 너무 길면 None."""
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")) or len(url) > URL_MAX:
        return None
    if any(ch.isspace() or ord(ch) < 32 or 127 <= ord(ch) <= 159 or ord(ch) in _INVISIBLE for ch in url):
        return None
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return None
    if not host or parts.username is not None or parts.password is not None:
        return None
    return url


def clean_area(out: AreaOut) -> dict:
    """AI 가 만든 분야 보고서를 저장할 모양으로 다듬는다 (링크 걸러내기, 개수 제한)."""
    trends = []
    for t in out.trends:
        headline = t.headline.strip()
        if not headline:
            continue
        by_url: dict[str, dict] = {}
        for s in t.sources:
            url = clean_url(s.url)
            if url is None:
                continue
            if url in by_url:
                by_url[url]["opened"] = by_url[url]["opened"] or bool(s.opened)
                continue
            by_url[url] = {"title": s.title.strip(), "url": url, "opened": bool(s.opened)}
        sources = list(by_url.values())
        if not sources:
            needs_check = [NO_SOURCE_NOTE]
        elif not any(s["opened"] for s in sources):
            needs_check = [UNOPENED_NOTE]
        else:
            needs_check = []
        trends.append({"headline": headline, "summary": t.summary.strip(), "sources": sources, "needs_check": needs_check})
    insights = [i.strip() for i in out.insights if i.strip()]
    return {"title": out.title.strip(), "trends": trends[:MAX_TRENDS], "insights": insights[:MAX_INSIGHTS]}


def clean_suggestions(db: Session, out: SuggestOut, knowledge_dir: Path) -> dict:
    """AI 가 만든 발굴 방향 제안을 저장할 모양으로 다듬는다 (없는 솔루션·출처 걸러내기, 개수 제한)."""
    wanted = {i for s in out.suggestions for i in s.solution_ids}
    existing = {i for (i,) in db.query(Solution.id).filter(Solution.id.in_(wanted)).all()} if wanted else set()
    suggestions = []
    for s in out.suggestions:
        title = s.title.strip()
        if not title:
            continue
        ids = list(dict.fromkeys(i for i in s.solution_ids if i in existing))
        kind = s.kind if s.kind in ("new", "push") else "new"
        if kind == "push" and not ids:
            kind = "new"
        sources, _missing = check_sources(knowledge_dir, s.sources)
        suggestions.append({"kind": kind, "title": title, "detail": s.detail.strip(), "reason": s.reason.strip(),
                            "solution_ids": ids, "sources": list(dict.fromkeys(sources))})
    insights = [i.strip() for i in out.insights if i.strip()]
    return {"suggestions": suggestions[:MAX_SUGGESTIONS], "insights": insights[:MAX_INSIGHTS]}


def start_run(db: Session, trigger: str, requester_id: int | None, slot: datetime | None = None) -> ResearchRun | None:
    """조사 실행을 시작한다. 이미 진행 중이면 None. 동시에 와도 하나만 만들어진다."""
    areas = {area: [t.id for t in items] for area, items in active_topics(db).items()}
    if not areas:
        raise ValueError(NO_TOPICS_MESSAGE)
    db.rollback()  # 읽기 트랜잭션을 끝낸다. 첫 문장이 쓰기여야 SQLite 쓰기 잠금을 먼저 잡는다.
    run = ResearchRun(trigger=trigger, slot=slot, requester_id=requester_id, status="running")
    db.add(run)
    db.flush()  # 쓰기 잠금: 다른 요청은 우리가 끝낼 때까지 기다린다.
    other = db.query(ResearchRun.id).filter(ResearchRun.status == "running", ResearchRun.id != run.id).first()
    if other is not None:
        db.rollback()
        return None
    if slot is not None and db.query(ResearchRun.id).filter(ResearchRun.slot == slot, ResearchRun.id != run.id).first() is not None:
        db.rollback()  # 같은 예정 시각을 이미 누가 맡았다.
        return None
    if slot is not None:  # 실행을 지워도 이 예정 시각은 다시 돌지 않는다
        marker = db.get(AppSetting, LAST_SLOT_KEY)
        if marker is None:
            db.add(AppSetting(key=LAST_SLOT_KEY, value=slot.isoformat()))
        elif marker.value < slot.isoformat():
            marker.value = slot.isoformat()
    for area in areas:
        report = ResearchReport(run_id=run.id, kind="area", area=area, status="pending")
        db.add(report)
        db.flush()
        job = AIJob(kind="research_area", input={"report_id": report.id}, requester_id=requester_id,
                    priority=10, status="pending")
        db.add(job)
        db.flush()
        report.job_id = job.id
    db.commit()
    return run


def sync_failed_reports(db: Session, run_id: int) -> None:
    """작업이 실패한 보고서(분야·제안)를 failed 로 맞추고 안내문을 채운다."""
    reports = db.query(ResearchReport).filter_by(run_id=run_id, status="pending").all()
    for r in reports:
        job = db.get(AIJob, r.job_id) if r.job_id else None
        if job is None or job.status != "failed":
            continue
        r.status = "failed"
        r.error = job.error if job.error_kind == "bad_output" and job.error else ERROR_KINDS.get(job.error_kind, ERROR_KINDS["unknown"])
    db.commit()


def advance_run(db: Session, run_id: int) -> None:
    """분야 보고서가 모두 끝나면 제안 작업을 한 번만 만들고, 제안까지 끝나면 실행을 마감한다.

    이미 마감된 실행에는 아무것도 하지 않는다. 마감된 실행에서 분야를 다시 시도해도 제안은 다시 만들지 않는다."""
    db.rollback()
    run = db.get(ResearchRun, run_id)
    if run is None or run.status != "running":
        return
    sync_failed_reports(db, run_id)
    db.refresh(run)
    areas = [r for r in run.reports if r.kind == "area"]
    if not areas or any(r.status not in ("done", "failed") for r in areas):
        return
    done = sum(r.status == "done" for r in areas)
    suggest = next((r for r in run.reports if r.kind == "suggest"), None)
    if suggest is None:
        if done == 0:
            _close_run(db, run_id, "failed")
            return
        suggest = _start_suggest(db, run)
    if suggest is None or suggest.status not in ("done", "failed"):
        return
    _close_run(db, run_id, "done" if done == len(areas) and suggest.status == "done" else "partial")


def _start_suggest(db: Session, run: ResearchRun) -> ResearchReport | None:
    """제안 보고서와 작업을 만든다. 동시에 불러도 하나만 남는다 (먼저 flush 한 쪽이 쓰기 잠금을 잡는다)."""
    run_id, requester_id = run.id, run.requester_id
    db.rollback()
    report = ResearchReport(run_id=run_id, kind="suggest", status="pending")
    db.add(report)
    db.flush()
    first = db.query(ResearchReport.id).filter_by(run_id=run_id, kind="suggest").order_by(ResearchReport.id).first()
    if first is None or first[0] != report.id:
        db.rollback()
        return db.query(ResearchReport).filter_by(run_id=run_id, kind="suggest").first()
    job = AIJob(kind="research_suggest", input={"report_id": report.id}, requester_id=requester_id, priority=10, status="pending")
    db.add(job)
    db.flush()
    report.job_id = job.id
    db.commit()
    return report


def recover_runs(db: Session) -> int:
    """시작할 때 진행 중으로 남은 실행을 마저 진행시킨다 (재시작으로 끊긴 작업이 있어도 실행이 멈춰 있지 않게)."""
    ids = [i for (i,) in db.query(ResearchRun.id).filter_by(status="running").all()]
    for run_id in ids:
        try:
            advance_run(db, run_id)
        except Exception:  # noqa: BLE001
            db.rollback()
            log.exception("조사 실행 %s 복구 실패", run_id)
    return len(ids)


def _close_run(db: Session, run_id: int, status: str) -> None:
    """조건부 UPDATE 라 두 작업이 동시에 마감하려 해도 한 번만 반영된다."""
    db.execute(update(ResearchRun).where(ResearchRun.id == run_id, ResearchRun.status == "running")
               .values(status=status, finished_at=datetime.now()))
    db.commit()


_ID_REF = re.compile(r"(\s*)(\(\s*)?(?<![A-Za-z0-9_])id\s*[:：]?\s*(\d{1,9})(?!\d)(\s*\))?", re.IGNORECASE)
_LEADING_NOTE = re.compile(r"^\s*[\(\[][^)\]]*[\)\]]\s*")
_TAIL_CHARS = 40


def _names_it(before: str, name: str) -> bool:
    """번호 바로 앞 글이 이미 그 솔루션 이름(또는 이름의 일부)을 말하고 있으면 True."""
    from app.ai.tasks import normalize_name  # 순환 참조를 피해 늦게 불러온다

    full = normalize_name(name)
    core = normalize_name(_LEADING_NOTE.sub("", name)) or full
    tail = normalize_name(before[-_TAIL_CHARS:])
    if not tail or not core:
        return False
    if tail.endswith(core) or tail.endswith(full):
        return True
    words = before.rstrip().split()
    for count in (2, 1):  # 이름을 줄여 쓴 경우: 앞 글의 마지막 한두 단어가 이름 안에 들어 있다
        last = normalize_name("".join(words[-count:]))
        if len(last) >= 3 and last in full:
            return True
    return False


def humanize_ids(db: Session, text: str) -> str:
    """화면에 낼 글에서 `id 3`, `(id 3)`, `ID: 3` 꼴의 솔루션 번호를 그 솔루션 이름으로 바꾼다. 없는 번호는 그대로 둔다.

    번호 바로 앞에서 이미 그 솔루션 이름을 말하고 있으면 이름이 겹치지 않게 번호만 지운다."""
    text = str(text or "")
    if not text:
        return text
    cache: dict[int, str | None] = {}

    def name_of(m: re.Match) -> str:
        sid = int(m.group(3))
        if sid not in cache:
            solution = db.get(Solution, sid)
            cache[sid] = solution.name if solution is not None else None
        name = cache[sid]
        if name is None:
            return m.group(0)
        space, opening, closing = m.group(1), m.group(2) or "", m.group(4) or ""
        if bool(opening) == bool(closing) and _names_it(m.string[: m.start()], name):
            return ""
        return f"{space}{opening.strip() and '('}{name}{closing.strip() and ')'}"

    return _ID_REF.sub(name_of, text)
