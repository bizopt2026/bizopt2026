"""제안 건 도우미."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app import checks
from app.checks import NO_EVIDENCE_NOTE as NO_SOURCE_NOTE, check_kind  # noqa: F401  (다른 곳에서 여기서 불러다 쓴다)
from app.models import AIJob, Proposal, Solution, User

if TYPE_CHECKING:
    from app.ai.proposal_tasks import DirectionsOut, Outline

MAX_DIRECTIONS = 3
SLIDE_KINDS = ("cover", "bullets", "two_column", "table", "steps", "closing")
MAX_STEPS = 5
MAX_SLIDES = 14
NO_COLUMNS = "표의 열 이름을 입력하세요."


def candidate_solutions(db: Session, p: Proposal) -> list[Solution]:
    """제안에 조합할 수 있는 솔루션: 박스 등록 단계 + 제안 건에서 따로 고른 것. 이름순, 중복 없음."""
    ids = [i for i in (p.extra_solution_ids or []) if isinstance(i, int)]
    cond = Solution.stage == "boxed"
    if ids:
        cond = or_(cond, Solution.id.in_(ids))
    return list(db.scalars(select(Solution).where(cond).order_by(Solution.name, Solution.id)))


def display_title(p: Proposal) -> str:
    if p.title:
        return p.title
    if p.business is not None:
        return f"{p.customer.name} · {p.business.name} 제안"
    return f"{p.customer.name} 제안"


def check_sources(knowledge_dir: Path, sources: list[str]) -> tuple[list[str], list[str]]:
    """출처 경로를 (존재하는 것, 존재하지 않는 것) 으로 나눈다. 지식 폴더 밖을 가리키는 경로는 없는 것으로 본다."""
    root = Path(knowledge_dir).resolve()
    found: list[str] = []
    missing: list[str] = []
    for source in sources:
        rel = source.strip().replace("\\", "/")
        parts = PurePosixPath(rel).parts  # `./` 와 중복 `/` 는 여기서 정리된다
        usable = (
            rel != "" and "\x00" not in rel
            and not rel.startswith("/") and not PureWindowsPath(rel).is_absolute() and not any(":" in part for part in parts)
            and ".." not in parts
        )
        if usable:
            try:
                target = (root / rel).resolve()
                usable = target.is_relative_to(root) and target.is_file()
            except (OSError, ValueError):
                usable = False
        (found if usable else missing).append(PurePosixPath(rel).as_posix() if usable else source)
    return found, missing


def clean_directions(db: Session, p: Proposal, out: "DirectionsOut", knowledge_dir: Path) -> list[dict]:
    """AI 가 만든 방향을 저장용 목록으로 바꾼다. 후보에 없는 솔루션과 확인되지 않은 출처는 빼고 `needs_check` 에 남긴다."""
    names = {s.id: s.name for s in candidate_solutions(db, p)}
    cleaned: list[dict] = []
    for d in out.directions:
        needs_check = [n for n in d.needs_check if n.strip()]
        solutions: list[dict] = []
        unknown = False
        for sol in d.solutions:
            if sol.solution_id not in names:
                unknown = True
            elif all(x["solution_id"] != sol.solution_id for x in solutions):
                solutions.append({"solution_id": sol.solution_id, "name": names[sol.solution_id], "role": sol.role})
        if unknown:
            needs_check.append(checks.UNKNOWN_SOLUTION_NOTE)
        if not solutions:
            continue
        rationale: list[dict] = []
        for ev in d.rationale:
            found, missing = check_sources(knowledge_dir, ev.sources)
            needs_check.extend(checks.source_unverified(m) for m in missing)
            if not found:
                needs_check.append(checks.no_evidence(ev.text))
            rationale.append({"text": ev.text, "sources": found})
        if not d.rationale:
            needs_check.append(checks.no_evidence())
        cleaned.append({
            "title": d.title, "summary": d.summary, "solutions": solutions, "rationale": rationale,
            "effects": list(d.effects), "needs_check": needs_check,
        })
    return cleaned[:MAX_DIRECTIONS]


def number_sources(direction: dict) -> dict:
    """방향 카드의 출처에 번호를 붙인다. 같은 경로는 카드 안에서 같은 번호(처음 나온 순서).
    `{"rationale": [{"text", "refs": [1, 2]}], "sources": [경로, ...]}` — 출처 없는 근거는 refs 가 빈 목록."""
    order: list[str] = []
    rationale = []
    for ev in direction.get("rationale") or []:
        if not isinstance(ev, dict):
            continue
        refs: list[int] = []
        for path in ev.get("sources") or []:
            if not isinstance(path, str):
                continue
            if path not in order:
                order.append(path)
            n = order.index(path) + 1
            if n not in refs:
                refs.append(n)
        rationale.append({"text": ev.get("text") or "", "refs": refs})
    return {"rationale": rationale, "sources": order}


def slide_sources(slide: dict) -> list[str]:
    """한 장의 출처 경로. 같은 경로는 한 번만, 처음 나온 순서대로. 장 카드의 출처 번호가 이 순서를 따른다."""
    return list(dict.fromkeys(x for x in slide.get("sources") or [] if isinstance(x, str)))


def _strip_list(items: list[str]) -> list[str]:
    return [x for x in (i.strip() for i in items) if x]


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def clean_outline(out: "Outline", knowledge_dir: Path) -> dict:
    """AI 구성안을 저장용 dict 로 바꾼다. 종류·표·단계·출처를 규칙에 맞게 고치고 빈 항목을 버린다."""
    slides: list[dict] = []
    for raw in out.slides:
        s = raw.model_dump()
        for key in ("kind", "title", "key_message", "subtitle", "left_title", "right_title"):
            s[key] = s[key].strip()
        s["kind"] = s["kind"].lower()
        for key in ("bullets", "left", "right", "needs_check"):
            s[key] = _strip_list(s[key])
        if s["kind"] not in SLIDE_KINDS:
            s["kind"] = "bullets"
        if s["kind"] == "cover" and slides:
            s["kind"] = "bullets"  # 표지는 맨 앞 한 장만
        s["columns"] = _strip_list(s["columns"])
        rows = [[c.strip() for c in row] for row in s["rows"]]
        rows = [row for row in rows if any(row)]
        if s["kind"] == "table" and not s["columns"]:
            s["kind"] = "bullets"
            s["bullets"] = s["bullets"] + [" / ".join(c for c in row if c) for row in rows]
        if s["kind"] == "table":
            n = len(s["columns"])
            s["rows"] = [(row + [""] * n)[:n] for row in rows]
        else:
            s["columns"], s["rows"] = [], []
        steps = [{"name": st["name"].strip(), "detail": st["detail"].strip()} for st in s["steps"]]
        s["steps"] = [st for st in steps if st["name"]][:MAX_STEPS]
        s["checked"] = []  # AI 가 새로 만든 구성안은 확인한 항목이 없다
        found, missing = check_sources(knowledge_dir, _strip_list(s["sources"]))
        s["sources"] = found
        s["needs_check"] = _unique(s["needs_check"] + [checks.source_unverified(m) for m in missing])
        if s["kind"] not in ("cover", "closing") and not s["sources"] and not s["needs_check"]:
            s["needs_check"] = [NO_SOURCE_NOTE]
        slides.append(s)
    title = out.title.strip()
    if not slides or slides[0]["kind"] != "cover":
        from app.ai.proposal_tasks import Slide  # 순환 참조를 피해 늦게 불러온다
        slides.insert(0, Slide(kind="cover", title=title).model_dump())
    return {"title": title, "slides": slides}


def _lines(text: str) -> list[str]:
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.split("|")]


def slide_from_form(kind: str, form) -> dict:
    """편집 폼 값을 슬라이드 dict 로 바꾼다. 그 종류에 속한 칸만 읽고 나머지는 비운다. 종류가 틀리면 ValueError."""
    if kind not in SLIDE_KINDS:
        raise ValueError(kind)
    from app.ai.proposal_tasks import Slide  # 순환 참조를 피해 늦게 불러온다

    def text(name: str) -> str:
        return str(form.get(name) or "").strip()

    s = Slide(kind=kind).model_dump()
    s["title"], s["key_message"] = text("title"), text("key_message")
    s["sources"], s["needs_check"] = _lines(form.get("sources")), _unique(_lines(form.get("needs_check")))
    if kind == "cover":
        s["subtitle"] = text("subtitle")
    elif kind == "bullets":
        s["bullets"] = _lines(form.get("bullets"))
    elif kind == "two_column":
        s["left_title"], s["right_title"] = text("left_title"), text("right_title")
        s["left"], s["right"] = _lines(form.get("left")), _lines(form.get("right"))
    elif kind == "table":
        columns = [c for c in _cells(str(form.get("columns") or "").replace("\n", " ")) if c]
        rows = [_cells(line) for line in _lines(form.get("rows"))]
        rows = [row for row in rows if any(row)]
        if rows and not columns:
            raise ValueError(NO_COLUMNS)
        s["columns"] = columns
        s["rows"] = [(row + [""] * len(columns))[: len(columns)] for row in rows]
    elif kind == "steps":
        steps = []
        for line in _lines(form.get("steps")):
            name, _, detail = line.partition(":")
            if name.strip():
                steps.append({"name": name.strip(), "detail": detail.strip()})
        s["steps"] = steps[:MAX_STEPS]
    return s


def slide_to_form(slide: dict) -> dict:
    """슬라이드 dict 를 편집 폼에 보일 문자열로 바꾼다 (slide_from_form 의 역변환)."""
    out = {k: slide.get(k, "") for k in ("title", "key_message", "subtitle", "left_title", "right_title")}
    for key in ("bullets", "left", "right", "sources", "needs_check"):
        out[key] = "\n".join(slide.get(key) or [])
    out["columns"] = " | ".join(slide.get("columns") or [])
    out["rows"] = "\n".join(" | ".join(row) for row in slide.get("rows") or [])
    out["steps"] = "\n".join(f"{st['name']}: {st['detail']}" if st["detail"] else st["name"] for st in slide.get("steps") or [])
    return out


def check_items(texts: list[str] | None, checked: list[str] | None = None) -> list[dict]:
    """"확인 필요" 문구를 화면용으로 바꾼다: 확인 안 한 근거 문제 → 확인 안 한 정할 값 → 확인한 것 순서(각각 원래 순서)."""
    done = set(checked or [])
    items = [{"index": n, "text": t, "kind": check_kind(t), "checked": t in done} for n, t in enumerate(texts or []) if isinstance(t, str)]
    return sorted(items, key=lambda i: (i["checked"], i["kind"] != "evidence"))


def slide_check_state(slide: dict) -> dict:
    """한 장의 확인 현황: 안 한 근거 문제·정할 값 개수, 확인한 개수, 카드에 쓸 단계(evidence / decide / clear)."""
    items = check_items(slide.get("needs_check"), slide.get("checked"))
    evidence = sum(1 for i in items if not i["checked"] and i["kind"] == "evidence")
    decide = sum(1 for i in items if not i["checked"] and i["kind"] == "decide")
    level = "evidence" if evidence else "decide" if decide else "clear"
    return {"evidence": evidence, "decide": decide, "done": sum(1 for i in items if i["checked"]), "level": level}


def outline_check_summary(outline: dict | None) -> dict:
    """구성안 전체의 장 수: evidence(근거 확인이 남은 장), decide(정할 값만 남은 장), done(확인을 끝낸 장), open(남은 장 전체)."""
    states = [slide_check_state(s) for s in (outline or {}).get("slides") or [] if isinstance(s, dict)]
    evidence = sum(1 for s in states if s["level"] == "evidence")
    decide = sum(1 for s in states if s["level"] == "decide")
    done = sum(1 for s in states if s["level"] == "clear" and s["done"])
    return {"evidence": evidence, "decide": decide, "done": done, "open": evidence + decide}


def check_summary_text(cs: dict) -> str:
    """"근거 문제 1장 · 확인할 점 2장" (0장인 부분은 뺀다). 남은 게 없으면 빈 글."""
    return " · ".join(p for p in (f"근거 문제 {cs['evidence']}장" if cs["evidence"] else "", f"확인할 점 {cs['decide']}장" if cs["decide"] else "") if p)


def keep_checked(old: dict, new: dict) -> dict:
    """고친 장에서 더 이상 없는 문구는 확인 목록에서도 뺀다."""
    wanted = new.get("needs_check") or []
    return {**new, "checked": [t for t in old.get("checked") or [] if t in wanted]}


def save_outline(db: Session, p: Proposal, slides: list[dict]) -> None:
    """구성안의 장 목록을 저장한다. 저장 시각은 늘 이전보다 커진다."""
    now = datetime.now()
    if p.outline_updated_at is not None and now <= p.outline_updated_at:
        now = p.outline_updated_at + timedelta(microseconds=1)
    p.outline = {**(p.outline or {}), "slides": slides}
    p.outline_updated_at = now
    if p.status == "deck":
        p.status = "outlined"  # 만든 제안서가 구성안과 달라졌다. 다시 만들어야 하는 건으로 되돌린다
    db.commit()


def reset_for_new_direction(p: Proposal, decks_dir: Path) -> None:
    """방향이 바뀌었을 때 구성안과 만든 제안서를 지우고, 이전 대화는 새 방향에 반영되지 않게 선을 긋는다. 커밋은 호출자가 한다."""
    deck = decks_dir / str(p.id) / p.deck_name if p.deck_name else None
    p.outline, p.outline_updated_at = None, None
    p.deck_name, p.deck_built_at = "", None
    p.chat_from_id = p.messages[-1].id if p.messages else None
    if deck is not None:
        deck.unlink(missing_ok=True)


def deck_is_stale(p: Proposal) -> bool:
    """만든 PPT 보다 구성안이 나중에 바뀌었으면 True."""
    return p.deck_built_at is not None and p.outline_updated_at is not None and p.outline_updated_at > p.deck_built_at


def start_job(db: Session, p: Proposal, kind: str, user: User, extra: dict | None = None) -> AIJob | None:
    """제안 건의 AI 작업을 시작한다. 이미 진행 중이면 None. 동시에 와도 하나만 들어간다."""
    db.refresh(p)
    if p.active_job is not None and p.active_job.status in ("pending", "running"):
        return None
    pid, previous = p.id, p.active_job_id
    db.rollback()  # 읽기 트랜잭션을 끝내고 쓰기를 새로 시작한다
    # 작업은 커밋 전에는 워커에게 보이지 않는다. 같은 트랜잭션에서 조건부 UPDATE 로 자리를 잡고, 졌으면 되돌린다.
    job = AIJob(kind=kind, input={"proposal_id": pid, **(extra or {})}, requester_id=user.id, priority=0, status="pending")
    db.add(job)
    db.flush()
    same = Proposal.active_job_id.is_(None) if previous is None else Proposal.active_job_id == previous
    claimed = db.execute(update(Proposal).where(Proposal.id == pid, same).values(active_job_id=job.id))
    if claimed.rowcount != 1:  # 다른 요청이 먼저 잡았다.
        db.rollback()
        return None
    db.commit()
    db.refresh(p)
    return job


STEPS = (("target", "대상"), ("directions", "방향"), ("chat", "구체화"), ("outline", "구성안"), ("deck", "제안서"))
_STATUS_STEP = {"new": "directions", "directions": "directions", "refining": "chat", "outlined": "outline", "deck": "deck"}
_JOB_STEP = {"proposal_directions": "directions", "proposal_chat": "chat", "proposal_outline": "outline"}
RECENT_MESSAGES = 6


def _has_selection(p: Proposal) -> bool:
    return p.selected_index is not None and 0 <= p.selected_index < len(p.directions or [])


def _has_outline(p: Proposal) -> bool:
    return bool(p.outline and p.outline.get("slides"))


def step_states(p: Proposal) -> list[dict]:
    """단계 표시줄의 칸. state: done(끝남) / current(지금 할 단계) / todo(갈 수 있음) / locked(아직 못 감, 링크 없음).

    지금 단계가 아닌 단계의 AI 작업이 돌고 있으면 그 칸은 busy(진행 중)이고, 아직 못 가는 단계여도 링크가 걸린다."""
    keys = [k for k, _ in STEPS]
    current = keys.index(_STATUS_STEP.get(p.status, "directions"))
    reachable = {"target": True, "directions": True, "chat": _has_selection(p), "outline": _has_outline(p), "deck": _has_outline(p)}
    working = open_section(p) if _job_running(p) else None
    steps = []
    for i, (key, label) in enumerate(STEPS):
        state = "done" if i < current else "current" if i == current else "todo" if reachable[key] else "locked"
        busy = working == key and i != current
        steps.append({
            "key": key, "label": label, "state": state, "busy": busy,
            "href": None if state == "locked" and not busy else f"#{key}",
        })
    return steps


def _job_running(p: Proposal) -> bool:
    job = p.active_job
    return job is not None and job.status in ("pending", "running") and job.kind in _JOB_STEP


def open_section(p: Proposal) -> str:
    """처음부터 펼쳐 둘 구역. 진행 중인 AI 작업이 있으면 그 작업의 구역, 아니면 상태가 가리키는 구역."""
    job = p.active_job
    if job is not None and job.status in ("pending", "running") and job.kind in _JOB_STEP:
        return _JOB_STEP[job.kind]
    return _STATUS_STEP.get(p.status, "directions")


def section_summaries(p: Proposal, candidates: list[Solution]) -> dict[str, str]:
    """접힌 구역에 보이는 한 줄 요약."""
    who = p.customer.name + (f" · {p.business.name}" if p.business is not None else "")
    names = [s.name for s in candidates]
    shown = ", ".join(names[:3]) + (f" 외 {len(names) - 3}개" if len(names) > 3 else "")
    target = f"{who} — 조합 솔루션 {shown}" if names else who
    directions = p.directions or []
    if _has_selection(p):
        direction = f"선택한 방향: {directions[p.selected_index]['title']}"
    else:
        direction = f"방향 {len(directions)}개" if directions else "아직 만들지 않음"
    if not _has_selection(p):
        chat = "방향을 선택하면 시작할 수 있습니다"
    elif p.messages:
        chat = f"대화 {len(p.messages)}개 · 마지막 메시지 {p.messages[-1].created_at:%Y-%m-%d %H:%M}"
    else:
        chat = "대화 없음"
    if _has_outline(p):
        slides = p.outline["slides"]
        left = check_summary_text(outline_check_summary(p.outline))
        outline = f"{len(slides)}장" + (f" · {left}" if left else "")
    else:
        outline = "아직 만들지 않음"
    deck = f"마지막 생성 {p.deck_built_at:%Y-%m-%d %H:%M}" if p.deck_name and p.deck_built_at else "아직 만들지 않음"
    return {"target": target, "directions": direction, "chat": chat, "outline": outline, "deck": deck}
