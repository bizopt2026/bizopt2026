"""출처 경로(지식 폴더 기준)를 사람이 읽는 이름과 화면 주소로 바꾼다."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app import labels
from app.knowledge import parse_rel
from app.models import Activity, Business, Customer, Partner, ResearchReport, Solution, SolutionFile

_SIMPLE = {
    "solution": (Solution, "솔루션", "/solutions/{id}"),
    "customer": (Customer, "고객", "/customers/{id}"),
    "business": (Business, "기존 사업", "/businesses/{id}"),
    "partner": (Partner, "파트너", "/partners/{id}"),
}


def _plain(path: str) -> dict:
    return {"label": path, "url": None, "kind": ""}


def _describe(db: Session, path: str) -> dict:
    parsed = parse_rel(path)
    if parsed is None:
        return _plain(path)
    kind, v = parsed
    if kind in _SIMPLE:
        model, kind_label, url = _SIMPLE[kind]
        obj = db.get(model, int(v["id"]))
        return {"label": obj.name, "url": url.format(id=obj.id), "kind": kind_label} if obj else _plain(path)
    if kind == "activity":
        a = db.get(Activity, int(v["id"]))
        if a is None:
            return _plain(path)
        title = a.title or "(제목 없음)"
        return {"label": f"{title} ({a.date.isoformat()})" if a.date else title, "url": f"/activities/{a.id}", "kind": "활동 기록"}
    if kind == "file":
        f = db.get(SolutionFile, int(v["id"]))
        sol = db.get(Solution, f.solution_id) if f is not None and str(f.solution_id) == v["solution_id"] else None
        if f is None or sol is None:
            return _plain(path)
        return {"label": f"{f.original_name} ({sol.name})", "url": f"/solutions/{sol.id}", "kind": "자료"}
    r = db.get(ResearchReport, int(v["id"]))  # report
    if r is None or (r.kind == "area") != (v["tag"] != "suggest"):
        return _plain(path)
    name = labels.RESEARCH_AREAS.get(r.area, r.area) if r.kind == "area" else "발굴 방향 제안"
    return {"label": f"리서치 {v['day']} {name}", "url": f"/research/runs/{r.run_id}", "kind": "리서치"}


def describe_source(db: Session, path: str) -> dict:
    """`{"label", "url", "kind"}`. 알아볼 수 없거나 없는 항목이면 경로 그대로(`url` 은 None). 예외를 던지지 않는다."""
    path = path if isinstance(path, str) else str(path)
    try:
        return _describe(db, path)
    except Exception:  # noqa: BLE001  화면 표시용이라 어떤 이유로도 페이지를 깨지 않는다
        return _plain(path)


class SourceLookup:
    """한 요청 안에서 같은 경로를 한 번만 조회한다. 템플릿에는 `src(path)` 로 쓴다."""

    def __init__(self, db: Session):
        self.db = db
        self._cache: dict[str, dict] = {}

    def __call__(self, path: str) -> dict:
        key = path if isinstance(path, str) else str(path)
        if key not in self._cache:
            self._cache[key] = describe_source(self.db, key)
        return self._cache[key]

    def labels(self, paths) -> dict[str, str]:
        return {p: self(p)["label"] for p in paths}
