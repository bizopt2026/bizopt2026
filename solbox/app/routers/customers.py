"""고객 화면."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.flash import flash
from app.db import get_db
from app.knowledge import KnowledgeMirror, get_mirror
from app.main import render
from app.models import Activity, Customer, User
from app.routers._common import (
    activities_linked_to, get_deletable_or_404, get_or_404, name_error, newest_first,
    resync_activities, resync_related, search_by_name,
)
from app.security import can_delete, require_user

router = APIRouter(prefix="/customers")
FIELDS = ("name", "industry", "contact", "memo")
HAS_BUSINESSES = "이 고객에 연결된 기존 사업이 있어 삭제할 수 없습니다. 사업을 먼저 삭제해 주세요."


HAS_PROPOSALS = "제안 건이 있는 고객은 삭제할 수 없습니다."


def _form_page(request: Request, form: dict, customer: Customer | None = None, error: str | None = None):
    return render(
        request, "customers/form.html", status_code=400 if error else 200,
        form=form, customer=customer, error=error,
    )


def _detail_page(request: Request, db: Session, user: User, customer: Customer, error: str | None = None):
    return render(
        request, "customers/detail.html", status_code=400 if error else 200,
        customer=customer, businesses=sorted(customer.businesses, key=lambda b: b.name),
        proposals=sorted(customer.proposals, key=lambda p: p.updated_at, reverse=True),
        activities=newest_first(activities_linked_to(db, Activity.customers, customer)),
        can_delete=can_delete(user, customer), error=error,
    )


@router.get("")
def list_customers(request: Request, q: str = "", user: User = Depends(require_user), db: Session = Depends(get_db)):
    return render(
        request, "customers/list.html", customers=search_by_name(db, Customer, q), q=q.strip()
    )


@router.get("/new")
def new_form(request: Request, user: User = Depends(require_user)):
    return _form_page(request, {f: "" for f in FIELDS})


@router.post("")
def create(
    request: Request,
    name: str = Form(""), industry: str = Form(""), contact: str = Form(""), memo: str = Form(""),
    user: User = Depends(require_user), db: Session = Depends(get_db),
    mirror: KnowledgeMirror = Depends(get_mirror),
):
    values = dict(name=name.strip(), industry=industry.strip(), contact=contact.strip(), memo=memo.strip())
    if error := name_error(name):
        return _form_page(request, values, error=error)
    customer = Customer(**values, created_by_id=user.id)
    db.add(customer)
    db.commit()
    mirror.sync(customer)
    flash(request, "고객을 등록했습니다.")
    return RedirectResponse(f"/customers/{customer.id}", status_code=303)


@router.get("/{customer_id}")
def detail(customer_id: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    return _detail_page(request, db, user, get_or_404(db, Customer, customer_id))


@router.get("/{customer_id}/edit")
def edit_form(customer_id: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    customer = get_or_404(db, Customer, customer_id)
    return _form_page(request, {f: getattr(customer, f) for f in FIELDS}, customer)


@router.post("/{customer_id}")
def update(
    customer_id: int, request: Request,
    name: str = Form(""), industry: str = Form(""), contact: str = Form(""), memo: str = Form(""),
    user: User = Depends(require_user), db: Session = Depends(get_db),
    mirror: KnowledgeMirror = Depends(get_mirror),
):
    customer = get_or_404(db, Customer, customer_id)
    values = dict(name=name.strip(), industry=industry.strip(), contact=contact.strip(), memo=memo.strip())
    if error := name_error(name):
        return _form_page(request, values, customer, error)
    for field, value in values.items():
        setattr(customer, field, value)
    db.commit()
    mirror.sync(customer)
    resync_related(db, mirror, customer)
    flash(request, "저장했습니다.")
    return RedirectResponse(f"/customers/{customer.id}", status_code=303)


@router.post("/{customer_id}/delete")
def delete(
    customer_id: int, request: Request,
    user: User = Depends(require_user), db: Session = Depends(get_db),
    mirror: KnowledgeMirror = Depends(get_mirror),
):
    customer = get_deletable_or_404(db, Customer, customer_id, user)
    if customer.businesses:
        return _detail_page(request, db, user, customer, HAS_BUSINESSES)
    if customer.proposals:
        return _detail_page(request, db, user, customer, HAS_PROPOSALS)
    activities = activities_linked_to(db, Activity.customers, customer)
    mirror.remove(customer)
    db.delete(customer)
    db.commit()
    resync_activities(db, mirror, activities)
    flash(request, "고객을 삭제했습니다.")
    return RedirectResponse("/customers", status_code=303)
