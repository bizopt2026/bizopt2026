"""기존 사업 화면."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.flash import flash
from app.db import get_db
from app.knowledge import KnowledgeMirror, get_mirror
from app.main import render
from app.models import Activity, Business, Customer, User
from app.routers._common import (
    activities_linked_to, get_deletable_or_404, get_or_404, name_error, newest_first,
    resync_activities, resync_related, search_by_name,
)
from app.security import can_delete, require_user

router = APIRouter(prefix="/businesses")
FIELDS = ("name", "customer_id", "description", "period", "status", "memo")
CUSTOMER_REQUIRED = "고객을 선택하세요."


def _form_page(
    request: Request, db: Session, form: dict, business: Business | None = None, error: str | None = None
):
    return render(
        request, "businesses/form.html", status_code=400 if error else 200,
        form=form, business=business, customers=search_by_name(db, Customer), error=error,
    )


def _parse(name, customer_id, description, period, status, memo, db: Session) -> tuple[dict, str | None]:
    """폼 값을 다듬고 오류 문구를 돌려준다. `customer_id` 는 존재하는 고객이어야 한다."""
    values = dict(
        name=name.strip(), customer_id=customer_id.strip(), description=description.strip(),
        period=period.strip(), status=status.strip(), memo=memo.strip(),
    )
    if error := name_error(name):
        return values, error
    if not values["customer_id"].isdigit() or db.get(Customer, int(values["customer_id"])) is None:
        return values, CUSTOMER_REQUIRED
    values["customer_id"] = int(values["customer_id"])
    return values, None


@router.get("")
def list_businesses(request: Request, q: str = "", user: User = Depends(require_user), db: Session = Depends(get_db)):
    businesses = search_by_name(db, Business, q, Customer.name, joins=(Business.customer,))
    return render(request, "businesses/list.html", businesses=businesses, q=q.strip())


@router.get("/new")
def new_form(
    request: Request, customer_id: str = "", user: User = Depends(require_user), db: Session = Depends(get_db)
):
    return _form_page(request, db, {**{f: "" for f in FIELDS}, "customer_id": customer_id})


@router.post("")
def create(
    request: Request,
    name: str = Form(""), customer_id: str = Form(""), description: str = Form(""),
    period: str = Form(""), status: str = Form(""), memo: str = Form(""),
    user: User = Depends(require_user), db: Session = Depends(get_db),
    mirror: KnowledgeMirror = Depends(get_mirror),
):
    values, error = _parse(name, customer_id, description, period, status, memo, db)
    if error:
        return _form_page(request, db, values, error=error)
    business = Business(**values, created_by_id=user.id)
    db.add(business)
    db.commit()
    db.refresh(business.customer)
    mirror.sync(business)
    mirror.sync(business.customer)  # 고객 사본의 사업 목록
    flash(request, "사업을 등록했습니다.")
    return RedirectResponse(f"/businesses/{business.id}", status_code=303)


@router.get("/{business_id}")
def detail(business_id: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    business = get_or_404(db, Business, business_id)
    return render(
        request, "businesses/detail.html", business=business,
        activities=newest_first(activities_linked_to(db, Activity.businesses, business)),
        proposals=sorted(business.proposals, key=lambda p: p.updated_at, reverse=True),
        can_delete=can_delete(user, business),
    )


@router.get("/{business_id}/edit")
def edit_form(business_id: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    business = get_or_404(db, Business, business_id)
    return _form_page(request, db, {f: getattr(business, f) for f in FIELDS}, business)


@router.post("/{business_id}")
def update(
    business_id: int, request: Request,
    name: str = Form(""), customer_id: str = Form(""), description: str = Form(""),
    period: str = Form(""), status: str = Form(""), memo: str = Form(""),
    user: User = Depends(require_user), db: Session = Depends(get_db),
    mirror: KnowledgeMirror = Depends(get_mirror),
):
    business = get_or_404(db, Business, business_id)
    values, error = _parse(name, customer_id, description, period, status, memo, db)
    if error:
        return _form_page(request, db, values, business, error)
    old_customer = business.customer
    for field, value in values.items():
        setattr(business, field, value)
    db.commit()
    db.refresh(business)
    mirror.sync(business)
    resync_related(db, mirror, business)  # 새 고객 사본의 사업 목록과 활동 기록
    if old_customer.id != business.customer_id:  # 옛 고객 사본의 사업 목록
        db.refresh(old_customer)
        mirror.sync(old_customer)
    flash(request, "저장했습니다.")
    return RedirectResponse(f"/businesses/{business.id}", status_code=303)


@router.post("/{business_id}/delete")
def delete(
    business_id: int, request: Request,
    user: User = Depends(require_user), db: Session = Depends(get_db),
    mirror: KnowledgeMirror = Depends(get_mirror),
):
    business = get_deletable_or_404(db, Business, business_id, user)
    customer = business.customer
    activities = activities_linked_to(db, Activity.businesses, business)
    mirror.remove(business)
    db.delete(business)  # 활동 기록과의 연결 행만 함께 지워진다
    db.commit()
    db.refresh(customer)
    mirror.sync(customer)
    resync_activities(db, mirror, activities)
    flash(request, "사업을 삭제했습니다.")
    return RedirectResponse("/businesses", status_code=303)
