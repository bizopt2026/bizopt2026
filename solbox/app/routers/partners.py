"""파트너 화면."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.flash import flash
from app.db import get_db
from app.knowledge import KnowledgeMirror, get_mirror
from app.main import render
from app.models import Activity, Partner, User
from app.routers._common import (
    activities_linked_to, get_deletable_or_404, get_or_404, name_error, newest_first,
    resync_activities, resync_related, search_by_name,
)
from app.security import can_delete, require_user

router = APIRouter(prefix="/partners")
FIELDS = ("name", "contact", "description", "memo")


def _form_page(request: Request, form: dict, partner: Partner | None = None, error: str | None = None):
    return render(
        request, "partners/form.html", status_code=400 if error else 200,
        form=form, partner=partner, error=error,
    )


@router.get("")
def list_partners(request: Request, q: str = "", user: User = Depends(require_user), db: Session = Depends(get_db)):
    return render(request, "partners/list.html", partners=search_by_name(db, Partner, q), q=q.strip())


@router.get("/new")
def new_form(request: Request, user: User = Depends(require_user)):
    return _form_page(request, {f: "" for f in FIELDS})


@router.post("")
def create(
    request: Request,
    name: str = Form(""), contact: str = Form(""), description: str = Form(""), memo: str = Form(""),
    user: User = Depends(require_user), db: Session = Depends(get_db),
    mirror: KnowledgeMirror = Depends(get_mirror),
):
    values = dict(name=name.strip(), contact=contact.strip(), description=description.strip(), memo=memo.strip())
    if error := name_error(name):
        return _form_page(request, values, error=error)
    partner = Partner(**values, created_by_id=user.id)
    db.add(partner)
    db.commit()
    mirror.sync(partner)
    flash(request, "파트너를 등록했습니다.")
    return RedirectResponse(f"/partners/{partner.id}", status_code=303)


@router.get("/{partner_id}")
def detail(partner_id: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    partner = get_or_404(db, Partner, partner_id)
    activities = newest_first(activities_linked_to(db, Activity.partners, partner))
    return render(
        request, "partners/detail.html", partner=partner, activities=activities,
        can_delete=can_delete(user, partner),
    )


@router.get("/{partner_id}/edit")
def edit_form(partner_id: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    partner = get_or_404(db, Partner, partner_id)
    return _form_page(request, {f: getattr(partner, f) for f in FIELDS}, partner)


@router.post("/{partner_id}")
def update(
    partner_id: int, request: Request,
    name: str = Form(""), contact: str = Form(""), description: str = Form(""), memo: str = Form(""),
    user: User = Depends(require_user), db: Session = Depends(get_db),
    mirror: KnowledgeMirror = Depends(get_mirror),
):
    partner = get_or_404(db, Partner, partner_id)
    values = dict(name=name.strip(), contact=contact.strip(), description=description.strip(), memo=memo.strip())
    if error := name_error(name):
        return _form_page(request, values, partner, error)
    for field, value in values.items():
        setattr(partner, field, value)
    db.commit()
    mirror.sync(partner)
    resync_related(db, mirror, partner)
    flash(request, "저장했습니다.")
    return RedirectResponse(f"/partners/{partner.id}", status_code=303)


@router.post("/{partner_id}/delete")
def delete(
    partner_id: int, request: Request,
    user: User = Depends(require_user), db: Session = Depends(get_db),
    mirror: KnowledgeMirror = Depends(get_mirror),
):
    partner = get_deletable_or_404(db, Partner, partner_id, user)
    solutions = list(partner.solutions)
    activities = activities_linked_to(db, Activity.partners, partner)
    for s in solutions:
        s.partner_id = None
    mirror.remove(partner)
    db.delete(partner)
    db.commit()
    for s in solutions:
        db.refresh(s)
        mirror.sync(s)
    resync_activities(db, mirror, activities)
    flash(request, "파트너를 삭제했습니다.")
    return RedirectResponse("/partners", status_code=303)
