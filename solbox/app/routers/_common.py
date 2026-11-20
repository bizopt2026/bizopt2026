"""항목 화면(파트너·고객·사업·솔루션 …)이 함께 쓰는 도우미."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence, TypeVar

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.knowledge import KnowledgeMirror
from app.models import Activity, Business, Customer, Partner, Solution, User
from app.security import ensure_can_delete

T = TypeVar("T")

NOT_FOUND = "항목을 찾을 수 없습니다."
NAME_REQUIRED = "이름을 입력하세요."


def get_or_404(db: Session, model: type[T], obj_id: int) -> T:
    obj = db.get(model, obj_id)
    if obj is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    return obj


def get_deletable_or_404(db: Session, model: type[T], obj_id: int, user: User) -> T:
    """없으면 404, 본인이 만든 것도 관리자도 아니면 403."""
    obj = get_or_404(db, model, obj_id)
    ensure_can_delete(user, obj)
    return obj


def search_by_name(db: Session, model, q: str = "", *extra_columns, joins=()) -> list:
    """이름(과 추가 칼럼)에 `q` 가 부분 일치하는 항목을 이름순으로 돌려준다. `q` 가 비면 전부."""
    stmt = select(model)
    for target in joins:
        stmt = stmt.join(target)
    q = q.strip()
    if q:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{escaped}%"
        stmt = stmt.where(or_(*(c.like(like, escape="\\") for c in (model.name, *extra_columns))))
    return list(db.scalars(stmt.order_by(model.name, model.id)).unique())


def name_error(name: str) -> str | None:
    """이름이 비었으면 화면에 보일 오류 문구를, 아니면 None."""
    return None if name.strip() else NAME_REQUIRED


def activities_linked_to(db: Session, relationship, obj) -> list[Activity]:
    """`relationship` (예: Activity.businesses) 으로 `obj` 에 연결된 활동 기록."""
    return list(db.scalars(select(Activity).where(relationship.any(id=obj.id))))


def resync_activities(db: Session, mirror: KnowledgeMirror, activities: list[Activity]) -> None:
    """연결 대상이 지워진 뒤 활동 기록 사본을 다시 쓴다."""
    for a in activities:
        db.refresh(a)
        mirror.sync(a)


def resync_related(db: Session, mirror: KnowledgeMirror, obj) -> None:
    """`obj` 의 이름이 적히는 다른 사본(상위·하위 항목, 연결된 활동 기록)을 다시 쓴다."""
    if isinstance(obj, Customer):
        related = [*obj.businesses, *activities_linked_to(db, Activity.customers, obj)]
    elif isinstance(obj, Business):
        related = [obj.customer, *activities_linked_to(db, Activity.businesses, obj)]
    elif isinstance(obj, Partner):
        related = [*obj.solutions, *activities_linked_to(db, Activity.partners, obj)]
    elif isinstance(obj, Solution):
        related = [*([obj.partner] if obj.partner else []), *activities_linked_to(db, Activity.solutions, obj)]
    else:
        return
    for item in related:
        db.refresh(item)
        mirror.sync(item)


def newest_first(activities) -> list[Activity]:
    """날짜 최신순. 날짜 없는 활동은 맨 뒤."""
    return sorted(
        activities,
        key=lambda a: (a.date is not None, a.date.toordinal() if a.date else 0, a.id),
        reverse=True,
    )


PER_PAGE = 50


@dataclass(frozen=True)
class Page:
    """목록 한 쪽. `items` 는 이 쪽의 항목, `total` 은 전체 건수."""
    items: list
    page: int
    pages: int
    total: int

    @property
    def has_pages(self) -> bool:
        return self.pages > 1


def paginate(items: Sequence, page, per_page: int = PER_PAGE) -> Page:
    """`items` 를 `per_page` 건씩 나눈 `page` 쪽 (1부터). 범위 밖이거나 숫자가 아니면 1쪽."""
    items = list(items)
    pages = max(1, -(-len(items) // per_page))
    try:
        number = int(page)
    except (TypeError, ValueError):
        number = 1
    if not 1 <= number <= pages:
        number = 1
    start = (number - 1) * per_page
    return Page(items=items[start:start + per_page], page=number, pages=pages, total=len(items))
