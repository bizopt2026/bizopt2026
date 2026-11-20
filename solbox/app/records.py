"""활동 기록과 솔루션 자료 파일을 저장하는 공용 로직. 화면(라우터)과 올리기 저장(`app.intake.apply`)이 함께 쓴다.

여기 함수는 커밋하지 않는다 (호출자가 커밋·롤백하고, 커밋 뒤에 지식 사본을 갱신한다)."""
from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import labels
from app.ai.tasks import normalize_name
from app.config import Settings
from app.extract import ExtractResult, extract_text
from app.intake import extension
from app.knowledge import KnowledgeMirror
from app.models import Activity, Business, Customer, Partner, Solution, SolutionFile, User

LINK_TYPES = {  # 연결 종류: (모델, Activity 관계 이름)
    "solution": (Solution, "solutions"),
    "customer": (Customer, "customers"),
    "business": (Business, "businesses"),
    "partner": (Partner, "partners"),
}
NEW_TYPES = {"solutions": Solution, "customers": Customer, "partners": Partner}


# ---- 솔루션 자료 파일 ----

def solution_file_path(settings: Settings, f: SolutionFile) -> Path:
    return settings.uploads_dir / "solutions" / str(f.solution_id) / f.stored_name


def new_solution_file(
    db: Session, settings: Settings, solution: Solution, name: str, kind: str, user: User,
    write: Callable[[Path], int | None], extracted: ExtractResult | None = None,
) -> tuple[SolutionFile, ExtractResult] | None:
    """자료 파일 한 건을 만든다: 행을 넣고(flush) `write(경로)` 로 파일을 쓰고 글을 읽는다. 커밋하지 않는다.

    `write` 는 쓴 바이트 수를, 크기 제한을 넘겨 쓰지 않았으면 None 을 돌려준다 (그러면 None; 행은 호출자가 롤백한다).
    `extracted` 가 있으면 파일에서 다시 읽지 않고 그 읽기 결과를 쓴다. 쓰다 실패하면 부분 파일을 지우고 예외를 그대로 올린다."""
    f = SolutionFile(solution_id=solution.id, original_name=name, kind=kind, created_by_id=user.id)
    db.add(f)
    db.flush()  # file_id 를 얻는다
    f.stored_name = f"{f.id}{extension(name)}"
    path = solution_file_path(settings, f)
    try:
        size = write(path)
        if size is None:
            return None
        f.size = size
        result = extracted if extracted is not None else extract_text(path)
        f.extract_status = result.status
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return f, result


# ---- 활동 기록 ----

def first_line(text: str, limit: int = 40) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:limit]
    return ""


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value.strip()) if len(value.strip()) == 10 else date.today()
    except ValueError:
        return date.today()


def valid_due(value: str) -> str:
    value = value.strip()
    try:
        date.fromisoformat(value)
    except ValueError:
        return ""
    return value if len(value) == 10 else ""


def rows(*columns: list[str]) -> list[tuple[str, ...]]:
    """같은 순서의 반복 필드를 행으로 묶는다. 짧은 열은 빈 문자열로 채운다."""
    count = max((len(c) for c in columns), default=0)
    return [tuple((c[i] if i < len(c) else "").strip() for c in columns) for i in range(count)]


def follow_ups(task, owner, due, done) -> list[dict]:
    flags = set(done)
    return [
        {"task": t, "owner": o, "due": valid_due(d), "done": str(i) in flags}
        for i, (t, o, d) in enumerate(rows(task, owner, due))
        if t
    ]


def demand_signals(customer, content) -> list[dict]:
    return [{"customer": c, "content": t} for c, t in rows(customer, content) if t]


def linked(a: Activity) -> list:
    return [obj for _, rel in LINK_TYPES.values() for obj in getattr(a, rel)]


def pick(db: Session, model, ids: list[str]) -> list:
    """id 문자열 목록에서 실제로 있는 항목만 (중복 없이) 고른다."""
    picked, seen = [], set()
    for raw in ids:
        if not (raw.strip().isascii() and raw.strip().isdecimal()) or int(raw) in seen:
            continue
        seen.add(int(raw))
        if (obj := db.get(model, int(raw))) is not None:
            picked.append(obj)
    return picked


def get_or_create(db: Session, model, name: str, user: User, **fields) -> tuple[object | None, bool]:
    """같은 이름(공백·문장부호·대소문자 무시, 완전 일치만)의 항목이 있으면 그것을, 없으면 `fields` 로 새로 만든다 (flush).
    (항목, 새로 만들었는지). 이름이 비면 (None, False)."""
    key = normalize_name(name)
    if not key:
        return None, False
    for obj in db.scalars(select(model).order_by(model.id)):
        if normalize_name(obj.name) == key:
            return obj, False
    extra = {"stage": "intake"} if model is Solution else {}
    obj = model(name=name.strip(), created_by_id=user.id, **extra, **fields)
    db.add(obj)
    db.flush()
    return obj, True


def find_or_create(db: Session, model, names: list[str], user: User) -> list:
    """이름만 가진 항목을 만든다. 같은 이름이 있으면 그것을 쓴다."""
    result = []
    for name in names:
        obj, _ = get_or_create(db, model, name, user)
        if obj is not None and obj not in result:
            result.append(obj)
    return result


def ambiguous_view(db: Session, model, items: list[dict]) -> list[dict]:
    """모호한 이름의 후보 id 를 화면에 보일 이름으로 바꾼다."""
    return [{"name": i["name"], "candidates": [o.name for c in i["candidates"] if (o := db.get(model, c)) is not None]}
            for i in items]


def apply_activity_fields(
    db: Session, user: User, a: Activity, *, kind, date_, title, raw_text, summary_md,
    fu_task, fu_owner, fu_due, fu_done, ds_customer, ds_content,
    solution_ids, customer_ids, business_ids, partner_ids, new_solutions, new_customers, new_partners,
) -> None:
    """폼 값을 활동 기록에 반영한다: 필드, 후속 조치, 수요 단서, 연결, 새로 등록할 이름. 커밋하지 않는다."""
    a.kind = kind if kind in labels.ACTIVITY_KINDS else "other"
    a.date = parse_date(date_)
    a.raw_text = raw_text
    a.title = title.strip() or first_line(raw_text) or first_line(summary_md)
    a.summary_md = summary_md.strip()
    a.follow_ups = follow_ups(fu_task, fu_owner, fu_due, fu_done)
    a.demand_signals = demand_signals(ds_customer, ds_content)
    ids = dict(solution=solution_ids, customer=customer_ids, business=business_ids, partner=partner_ids)
    for t, (model, rel) in LINK_TYPES.items():
        setattr(a, rel, pick(db, model, ids[t]))
    for rel, names in (("solutions", new_solutions), ("customers", new_customers), ("partners", new_partners)):
        current = getattr(a, rel)
        for obj in find_or_create(db, NEW_TYPES[rel], names, user):
            if obj not in current:
                current.append(obj)


def sync_all(mirror: KnowledgeMirror, a: Activity, extra: list = ()) -> None:
    """활동 기록과 거기 연결된(또는 `extra` 로 준) 항목의 지식 사본을 다시 쓴다."""
    mirror.sync(a)
    seen = set()
    for obj in [*linked(a), *extra]:
        if (type(obj), obj.id) not in seen:
            seen.add((type(obj), obj.id))
            mirror.sync(obj)
