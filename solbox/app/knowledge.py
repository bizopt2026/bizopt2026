"""DB 항목의 Markdown 사본을 knowledge 폴더에 쓴다 (Claude CLI 가 읽는 용도)."""
from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from app import checks, labels
from app.extract import extract_text
from app.models import Activity, Business, Customer, Partner, ResearchReport, Solution, SolutionFile

_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')


# 사본 경로 형식. 경로를 쓰는 `_rel` 과 읽는 `parse_rel` 이 같은 표를 쓴다.
REL_FORMATS = {
    "solution": "solutions/{id}/card.md",
    "customer": "customers/{id}.md",
    "business": "businesses/{id}.md",
    "partner": "partners/{id}.md",
    "activity": "activities/{year}/{id}.md",
    "file": "solutions/{solution_id}/files/{id}-{name}.md",
    "report": "research/{day}-{tag}-{id}.md",
}
_REL_FIELDS = {
    "id": r"[0-9]{1,18}", "solution_id": r"[0-9]{1,18}", "year": r"[0-9]{4}|undated", "day": r"[0-9]{4}-[0-9]{2}-[0-9]{2}",
    "tag": r"[^/-]+", "name": r"[^/]+",
}


def _rel_regex(fmt: str) -> re.Pattern:
    parts = re.split(r"\{(\w+)\}", fmt)
    body = "".join(re.escape(x) if i % 2 == 0 else f"(?P<{x}>{_REL_FIELDS[x]})" for i, x in enumerate(parts))
    return re.compile(body + r"\Z")


_REL_PATTERNS = {kind: _rel_regex(fmt) for kind, fmt in REL_FORMATS.items()}


def parse_rel(path: str) -> tuple[str, dict[str, str]] | None:
    """사본 경로를 (항목 종류, 경로 안의 값들) 로 읽는다. 형식이 맞지 않으면 None."""
    path = str(path or "").strip().replace("\\", "/")
    for kind, pattern in _REL_PATTERNS.items():
        m = pattern.match(path)
        if m:
            return kind, m.groupdict()
    return None


def safe_name(name: str) -> str:
    """파일 이름에 쓸 수 없는 글자를 `_` 로 바꾸고 80자로 자른다."""
    cleaned = _UNSAFE.sub("_", name).strip(" .")[:80].strip(" .")
    return cleaned or "file"


def file_markdown(f: SolutionFile, text: str) -> str:
    """자료 파일에서 뽑은 본문을 사본 형식으로 만든다."""
    return f"# {f.original_name} ({labels.FILE_KINDS.get(f.kind, f.kind)})\n\n{text}\n"


def _item(label: str, value) -> str | None:
    text = "" if value is None else str(value).strip()
    return f"- {label}: " + text.replace("\n", "\n  ") if text else None


def _inline(text) -> str:
    """웹에서 온 글을 한 줄로 만든다: 줄바꿈·탭은 공백 하나로, 링크를 깨는 대괄호는 뺀다."""
    return re.sub(r"\s+", " ", str(text or "")).replace("[", "").replace("]", "").strip()


def _quote(text: str) -> list[str]:
    """여러 줄 글을 인용문으로 만든다. 웹에서 온 글이 제목 줄로 시작할 수 없다."""
    return [f"> {line}".rstrip() for line in str(text).strip().splitlines()]


def _lines(*items: str | None) -> list[str]:
    return [i for i in items if i]


def _section(title: str, body: str) -> list[str]:
    body = (body or "").strip()
    return ["", f"## {title}", "", body] if body else []


class KnowledgeMirror:
    def __init__(self, root: Path):
        self.root = Path(root)

    # ----- 경로 -----
    def _rel(self, obj) -> str:
        if isinstance(obj, Solution):
            return REL_FORMATS["solution"].format(id=obj.id)
        if isinstance(obj, Customer):
            return REL_FORMATS["customer"].format(id=obj.id)
        if isinstance(obj, Business):
            return REL_FORMATS["business"].format(id=obj.id)
        if isinstance(obj, Partner):
            return REL_FORMATS["partner"].format(id=obj.id)
        if isinstance(obj, Activity):
            return REL_FORMATS["activity"].format(year=obj.date.year if obj.date else "undated", id=obj.id)
        if isinstance(obj, SolutionFile):
            return REL_FORMATS["file"].format(solution_id=obj.solution_id, id=obj.id, name=safe_name(obj.original_name))
        if isinstance(obj, ResearchReport):
            day = (obj.created_at or datetime.now()).date().isoformat()
            return REL_FORMATS["report"].format(day=day, tag=obj.area if obj.kind == "area" else "suggest", id=obj.id)
        raise TypeError(f"지원하지 않는 항목: {type(obj).__name__}")

    def rel_path(self, obj) -> str:
        return self._rel(obj)

    def path_for(self, obj) -> Path:
        return self.root / self._rel(obj)

    def _ref(self, name: str, obj) -> str:
        return f"{name} ({self._rel(obj)})"

    # ----- 쓰기·지우기 -----
    def _write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def sync(self, obj) -> None:
        if isinstance(obj, Activity):
            for old in (self.root / "activities").glob(f"*/{obj.id}.md"):
                old.unlink(missing_ok=True)
        render = {
            Solution: self._solution,
            Customer: self._customer,
            Business: self._business,
            Partner: self._partner,
            Activity: self._activity,
            ResearchReport: self._report,
        }.get(type(obj))
        if render is None:
            raise TypeError(f"지원하지 않는 항목: {type(obj).__name__}")
        self._write(self.path_for(obj), "\n".join(render(obj)).rstrip() + "\n")

    def remove(self, obj) -> None:
        if isinstance(obj, Solution):
            shutil.rmtree(self.root / "solutions" / str(obj.id), ignore_errors=True)
        elif isinstance(obj, Activity):
            for old in (self.root / "activities").glob(f"*/{obj.id}.md"):
                old.unlink(missing_ok=True)
        else:
            self.path_for(obj).unlink(missing_ok=True)

    def write_file_text(self, f: SolutionFile, text: str) -> None:
        self._write(self.path_for(f), text)

    def remove_file_text(self, f: SolutionFile) -> None:
        self.path_for(f).unlink(missing_ok=True)

    def rebuild_all(self, db: Session, uploads_dir: Path) -> None:
        """사본 폴더를 비우고 DB 의 모든 항목과 자료 텍스트를 새로 쓴다."""
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)
        for model in (Solution, Customer, Business, Partner, Activity):
            for obj in db.scalars(select(model).order_by(model.id)):
                self.sync(obj)
        for report in db.scalars(select(ResearchReport).where(ResearchReport.status == "done").order_by(ResearchReport.id)):
            self.sync(report)
        for f in db.scalars(select(SolutionFile).order_by(SolutionFile.id)):
            result = extract_text(Path(uploads_dir) / "solutions" / str(f.solution_id) / f.stored_name)
            f.extract_status = result.status
            if result.status == "ok":
                self.write_file_text(f, file_markdown(f, result.text))
        db.commit()

    # ----- 항목별 본문 -----
    def _activity_log(self, activities: list[Activity]) -> list[str]:
        if not activities:
            return []
        # 최신순, 날짜 없는 활동은 맨 뒤
        ordered = sorted(
            activities,
            key=lambda a: (a.date is not None, a.date.toordinal() if a.date else 0, a.id),
            reverse=True,
        )
        out = ["", "## 활동 기록", ""]
        for a in ordered:
            when = a.date.isoformat() if a.date else "날짜 없음"
            out.append(f"- {when} {a.title or '(제목 없음)'} ({self._rel(a)})")
        return out

    def _linked_activities(self, obj, column) -> list[Activity]:
        session = object_session(obj)
        if session is None:
            return []
        return list(session.scalars(select(Activity).where(column.any(id=obj.id))))

    def _solution(self, s: Solution) -> list[str]:
        out = [f"# {s.name}", ""]
        out += _lines(
            _item("단계", labels.STAGES.get(s.stage, s.stage)),
            f"- 파트너: {self._ref(s.partner.name, s.partner)}" if s.partner else None,
            _item("태그", s.tags),
            _item("한 줄 요약", s.summary),
            _item("활용 방향", s.usage_direction),
            _item("적용 대상", s.applicable_types),
        )
        out += _section("설명", s.description)
        if s.files:
            out += ["", "## 자료", ""]
            out += [
                f"- {labels.FILE_KINDS.get(f.kind, f.kind)}: {f.original_name} ({self._rel(f)})"
                for f in sorted(s.files, key=lambda f: f.id)
            ]
        return out + self._activity_log(list(s.activities))

    def _customer(self, c: Customer) -> list[str]:
        out = [f"# {c.name}", ""]
        out += _lines(_item("업종", c.industry), _item("연락처", c.contact), _item("메모", c.memo))
        if c.businesses:
            out += ["", "## 사업", ""]
            out += [f"- {self._ref(b.name, b)}" for b in sorted(c.businesses, key=lambda b: b.id)]
        return out + self._activity_log(self._linked_activities(c, Activity.customers))

    def _business(self, b: Business) -> list[str]:
        out = [f"# {b.name}", ""]
        out += _lines(
            f"- 고객: {self._ref(b.customer.name, b.customer)}" if b.customer else None,
            _item("기간", b.period),
            _item("상태", b.status),
            _item("설명", b.description),
            _item("메모", b.memo),
        )
        return out + self._activity_log(self._linked_activities(b, Activity.businesses))

    def _partner(self, p: Partner) -> list[str]:
        out = [f"# {p.name}", ""]
        out += _lines(_item("연락처", p.contact), _item("소개", p.description), _item("메모", p.memo))
        if p.solutions:
            out += ["", "## 솔루션", ""]
            out += [f"- {self._ref(s.name, s)}" for s in sorted(p.solutions, key=lambda s: s.id)]
        return out + self._activity_log(self._linked_activities(p, Activity.partners))

    def _activity(self, a: Activity) -> list[str]:
        out = [f"# {a.title or '(제목 없음)'}", ""]
        out += _lines(
            _item("종류", labels.ACTIVITY_KINDS.get(a.kind, a.kind)),
            _item("날짜", a.date.isoformat() if a.date else ""),
            _item("솔루션", ", ".join(self._ref(s.name, s) for s in a.solutions)),
            _item("고객", ", ".join(self._ref(c.name, c) for c in a.customers)),
            _item("사업", ", ".join(self._ref(b.name, b) for b in a.businesses)),
            _item("파트너", ", ".join(self._ref(p.name, p) for p in a.partners)),
        )
        out += _section("정리", a.summary_md)
        follow = []
        for fu in a.follow_ups or []:
            mark = "x" if fu.get("done") else " "
            extra = ", ".join(
                p for p in (
                    f"담당 {fu['owner']}" if fu.get("owner") else "",
                    f"기한 {fu['due']}" if fu.get("due") else "",
                ) if p
            )
            follow.append(f"- [{mark}] {fu.get('task', '')}" + (f" ({extra})" if extra else ""))
        out += _section("후속 조치", "\n".join(follow))
        signals = [
            f"- {d.get('customer', '')}: {d.get('content', '')}".replace("- : ", "- ")
            for d in a.demand_signals or []
        ]
        out += _section("수요 단서", "\n".join(signals))
        return out + _section("원문", a.raw_text)

    def _report(self, r: ResearchReport) -> list[str]:
        is_area = r.kind == "area"
        out = [f"# {r.title or ('리서치 보고서' if is_area else '발굴 방향 제안')}", ""]
        out += _lines(
            _item("분야", labels.RESEARCH_AREAS.get(r.area, r.area)) if is_area else _item("종류", "발굴 방향 제안"),
            _item("작성일", r.created_at.date().isoformat() if r.created_at else ""),
        )
        if is_area:
            out += ["", "## 동향"]
            for t in r.trends or []:
                out += ["", f"### {_inline(t.get('headline'))}"]
                if str(t.get("summary") or "").strip():
                    out += [""] + _quote(t["summary"])
                links = [
                    f"- [{_inline(s.get('title')) or _inline(s.get('url'))}]({s.get('url', '')})"
                    + ("" if s.get("opened", True) else " (검색 결과 링크, 본문 미확인)")
                    for s in t.get("sources", [])
                ]
                out += ([""] + links) if links else ["", f"- {checks.NO_LINK_NOTE}"]
        else:
            out += ["", "## 발굴 방향 제안"]
            for sug in r.suggestions or []:
                kind = labels.SUGGESTION_KINDS.get(sug.get("kind"), sug.get("kind", ""))
                out += ["", f"### [{kind}] {sug.get('title', '')}", ""]
                out += _lines(
                    _item("내용", sug.get("detail")),
                    _item("이유", sug.get("reason")),
                    _item("대상 솔루션 id", ", ".join(str(i) for i in sug.get("solution_ids", []))),
                    _item("근거", ", ".join(sug.get("sources", []))),
                )
        return out + _section("시사점", "\n".join(f"- {_inline(i)}" for i in r.insights or []))


def get_mirror(request: Request) -> KnowledgeMirror:
    return request.app.state.mirror
