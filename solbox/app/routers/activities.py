"""활동 기록 화면."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.flash import flash
from app import labels
from app.ai.queue import enqueue, queue_position
from app.ai.tasks import ActivityDraft, draft_summary_md, match_mentions, normalize_draft, normalize_name
from app.db import get_db
from app.extract import decode_text
from app.knowledge import KnowledgeMirror, get_mirror
from app.main import render
from app.models import AIJob, Activity, Business, Customer, Partner, Solution, User
from app.routers._common import get_deletable_or_404, get_or_404, newest_first
from app.security import can_delete, require_user

router = APIRouter(prefix="/activities")

MEMO_REQUIRED = "메모를 입력하세요."
BAD_MEMO_FILE = "텍스트 파일(.txt, .md)만 올릴 수 있습니다."
LINK_TYPES = {  # 연결 종류: (모델, Activity 관계 이름)
    "solution": (Solution, "solutions"),
    "customer": (Customer, "customers"),
    "business": (Business, "businesses"),
    "partner": (Partner, "partners"),
}
NEW_TYPES = {"solutions": Solution, "customers": Customer, "partners": Partner}


def read_memo(raw_text: str, memo_file: UploadFile | None) -> str:
    """입력창 글과 파일 내용을 빈 줄 하나로 이어 붙인다. 둘 다 비면 400."""
    parts = [raw_text.strip()]
    if memo_file is not None and memo_file.filename:
        if Path(memo_file.filename).suffix.lower() not in (".txt", ".md"):
            raise HTTPException(status_code=400, detail=BAD_MEMO_FILE)
        parts.append(decode_text(memo_file.file.read()).strip())
    text = "\n\n".join(p for p in parts if p)
    if not text:
        raise HTTPException(status_code=400, detail=MEMO_REQUIRED)
    return text


def _first_line(text: str, limit: int = 40) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:limit]
    return ""


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value.strip()) if len(value.strip()) == 10 else date.today()
    except ValueError:
        return date.today()


def _valid_due(value: str) -> str:
    value = value.strip()
    try:
        date.fromisoformat(value)
    except ValueError:
        return ""
    return value if len(value) == 10 else ""


def _rows(*columns: list[str]) -> list[tuple[str, ...]]:
    """같은 순서의 반복 필드를 행으로 묶는다. 짧은 열은 빈 문자열로 채운다."""
    count = max((len(c) for c in columns), default=0)
    return [tuple((c[i] if i < len(c) else "").strip() for c in columns) for i in range(count)]


def _follow_ups(task, owner, due, done) -> list[dict]:
    flags = set(done)
    return [
        {"task": t, "owner": o, "due": _valid_due(d), "done": str(i) in flags}
        for i, (t, o, d) in enumerate(_rows(task, owner, due))
        if t
    ]


def _demand_signals(customer, content) -> list[dict]:
    return [{"customer": c, "content": t} for c, t in _rows(customer, content) if t]


def _linked(a: Activity) -> list:
    return [obj for _, rel in LINK_TYPES.values() for obj in getattr(a, rel)]


def _pick(db: Session, model, ids: list[str]) -> list:
    picked, seen = [], set()
    for raw in ids:
        if not raw.strip().isdigit() or int(raw) in seen:
            continue
        seen.add(int(raw))
        if (obj := db.get(model, int(raw))) is not None:
            picked.append(obj)
    return picked


def _find_or_create(db: Session, model, names: list[str], user: User) -> list:
    """이름만 가진 항목을 만든다. 같은 이름(공백·문장부호·대소문자 무시, 완전 일치만)이 있으면 그것을 쓴다."""
    existing: dict[str, object] = {}
    for o in db.scalars(select(model).order_by(model.id)):
        existing.setdefault(normalize_name(o.name), o)
    result = []
    for name in names:
        key = normalize_name(name)
        if not key:
            continue
        if key not in existing:
            extra = {"stage": "intake"} if model is Solution else {}
            existing[key] = model(name=name.strip(), created_by_id=user.id, **extra)
            db.add(existing[key])
        if existing[key] not in result:
            result.append(existing[key])
    db.flush()
    return result


def _choices(db: Session) -> dict:
    return {
        "solution": list(db.scalars(select(Solution).order_by(Solution.name))),
        "customer": list(db.scalars(select(Customer).order_by(Customer.name))),
        "business": list(db.scalars(select(Business).order_by(Business.name))),
        "partner": list(db.scalars(select(Partner).order_by(Partner.name))),
    }


def blank_values() -> dict:
    """`activities/_form_fields.html` 이 받는 값 묶음의 기본형."""
    return {
        "kind": "meeting", "date": date.today().isoformat(), "title": "", "raw_text": "", "summary_md": "",
        "follow_ups": [], "demand_signals": [],
        "selected": {t: [] for t in LINK_TYPES},
        "new_names": {t: [] for t in NEW_TYPES},        # 체크된 채로 보이는 새 이름
        "new_candidates": {t: [] for t in NEW_TYPES},   # 체크 해제로 보이는 새 이름 후보 (AI 초안)
    }


def form_values(a: Activity) -> dict:
    values = blank_values()
    values.update(
        kind=a.kind, date=a.date.isoformat() if a.date else "", title=a.title, raw_text=a.raw_text,
        summary_md=a.summary_md, follow_ups=list(a.follow_ups or []), demand_signals=list(a.demand_signals or []),
        selected={t: [o.id for o in getattr(a, rel)] for t, (_, rel) in LINK_TYPES.items()},
    )
    return values


def _form_page(request: Request, db: Session, values: dict, activity: Activity | None = None, error: str | None = None):
    return render(
        request, "activities/form.html", status_code=400 if error else 200,
        activity=activity, f=values, choices=_choices(db), error=error,
    )


def _submitted_values(
    kind, date_, title, raw_text, summary_md, fu_task, fu_owner, fu_due, ds_customer, ds_content,
    solution_ids, customer_ids, business_ids, partner_ids, new_solutions, new_customers, new_partners,
) -> dict:
    """제출된 값을 오류 화면에 다시 보여 줄 형태로 모은다."""
    ids = dict(solution=solution_ids, customer=customer_ids, business=business_ids, partner=partner_ids)
    return {
        "kind": kind, "date": date_, "title": title, "raw_text": raw_text, "summary_md": summary_md,
        "follow_ups": [{"task": t, "owner": o, "due": d, "done": False} for t, o, d in _rows(fu_task, fu_owner, fu_due)],
        "demand_signals": [{"customer": c, "content": t} for c, t in _rows(ds_customer, ds_content)],
        "selected": {t: [int(i) for i in v if i.strip().isdigit()] for t, v in ids.items()},
        "new_names": {
            rel: [n.strip() for n in names if n.strip()]
            for rel, names in (("solutions", new_solutions), ("customers", new_customers), ("partners", new_partners))
        },
        "new_candidates": {t: [] for t in NEW_TYPES},
    }


def _apply_form(
    db: Session, user: User, a: Activity, *, kind, date_, title, raw_text, summary_md,
    fu_task, fu_owner, fu_due, fu_done, ds_customer, ds_content,
    solution_ids, customer_ids, business_ids, partner_ids, new_solutions, new_customers, new_partners,
) -> None:
    a.kind = kind if kind in labels.ACTIVITY_KINDS else "other"
    a.date = _parse_date(date_)
    a.raw_text = raw_text
    a.title = title.strip() or _first_line(raw_text)
    a.summary_md = summary_md.strip()
    a.follow_ups = _follow_ups(fu_task, fu_owner, fu_due, fu_done)
    a.demand_signals = _demand_signals(ds_customer, ds_content)
    ids = dict(solution=solution_ids, customer=customer_ids, business=business_ids, partner=partner_ids)
    for t, (model, rel) in LINK_TYPES.items():
        setattr(a, rel, _pick(db, model, ids[t]))
    for rel, names in (("solutions", new_solutions), ("customers", new_customers), ("partners", new_partners)):
        current = getattr(a, rel)
        for obj in _find_or_create(db, NEW_TYPES[rel], names, user):
            if obj not in current:
                current.append(obj)


def _sync_all(mirror: KnowledgeMirror, a: Activity, extra: list = ()) -> None:
    mirror.sync(a)
    seen = set()
    for obj in [*_linked(a), *extra]:
        if (type(obj), obj.id) not in seen:
            seen.add((type(obj), obj.id))
            mirror.sync(obj)


def _save(request, db, user, mirror, activity: Activity | None, raw_text, memo_file, kind, date_, title, summary_md,
          fu_task, fu_owner, fu_due, fu_done, ds_customer, ds_content, solution_ids, customer_ids, business_ids,
          partner_ids, new_solutions, new_customers, new_partners):
    submitted = _submitted_values(
        kind, date_, title, raw_text, summary_md, fu_task, fu_owner, fu_due, ds_customer, ds_content,
        solution_ids, customer_ids, business_ids, partner_ids, new_solutions, new_customers, new_partners,
    )
    try:
        text = read_memo(raw_text, memo_file)
    except HTTPException as e:
        return _form_page(request, db, submitted, activity, e.detail)
    old = _linked(activity) if activity is not None else []
    if activity is None:
        activity = Activity(created_by_id=user.id, raw_text=text)
        db.add(activity)
    _apply_form(
        db, user, activity, kind=kind, date_=date_, title=title, raw_text=text, summary_md=summary_md,
        fu_task=fu_task, fu_owner=fu_owner, fu_due=fu_due, fu_done=fu_done,
        ds_customer=ds_customer, ds_content=ds_content, solution_ids=solution_ids, customer_ids=customer_ids,
        business_ids=business_ids, partner_ids=partner_ids,
        new_solutions=new_solutions, new_customers=new_customers, new_partners=new_partners,
    )
    db.commit()
    _sync_all(mirror, activity, old)
    flash(request, "활동 기록을 저장했습니다.")
    return RedirectResponse(f"/activities/{activity.id}", status_code=303)


@router.get("")
def list_activities(
    request: Request, q: str = "", kind: str = "", user: User = Depends(require_user), db: Session = Depends(get_db),
):
    stmt = select(Activity)
    q = q.strip()
    if q:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{escaped}%"
        stmt = stmt.where(or_(*(c.like(like, escape="\\") for c in (Activity.title, Activity.raw_text, Activity.summary_md))))
    if kind in labels.ACTIVITY_KINDS:
        stmt = stmt.where(Activity.kind == kind)
    return render(request, "activities/list.html", activities=newest_first(db.scalars(stmt)), q=q, kind=kind)


@router.get("/new")
def new_form(request: Request, user: User = Depends(require_user)):
    return render(request, "activities/new.html", error=None, raw_text="")


@router.post("/raw")
def save_raw(
    request: Request, raw_text: str = Form(""), memo_file: UploadFile | None = None,
    user: User = Depends(require_user), db: Session = Depends(get_db),
    mirror: KnowledgeMirror = Depends(get_mirror),
):
    try:
        text = read_memo(raw_text, memo_file)
    except HTTPException as e:
        return render(request, "activities/new.html", status_code=400, error=e.detail, raw_text=raw_text)
    activity = Activity(
        kind="meeting", date=date.today(), title=_first_line(text), raw_text=text, summary_md="",
        follow_ups=[], demand_signals=[], created_by_id=user.id,
    )
    db.add(activity)
    db.commit()
    mirror.sync(activity)
    flash(request, "원문을 저장했습니다. 수정에서 정리할 수 있습니다.")
    return RedirectResponse(f"/activities/{activity.id}", status_code=303)


DRAFT_KIND = "activity_structure"


def _draft_job(db: Session, job_id: int, user: User) -> AIJob:
    job = get_or_404(db, AIJob, job_id)
    if job.kind != DRAFT_KIND:
        raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.")
    if job.requester_id != user.id:
        raise HTTPException(status_code=403, detail="본인이 요청한 초안만 볼 수 있습니다.")
    return job


def _ambiguous_view(db: Session, model, items: list[dict]) -> list[dict]:
    """모호한 이름의 후보 id 를 화면에 보일 이름으로 바꾼다."""
    return [{"name": i["name"], "candidates": [o.name for c in i["candidates"] if (o := db.get(model, c)) is not None]}
            for i in items]


def draft_values(db: Session, d: ActivityDraft, raw_text: str) -> dict:
    """AI 초안을 정리 폼 값으로 바꾼다. 새 이름은 체크 해제 후보로 둔다."""
    d = normalize_draft(d, date.today())
    found = match_mentions(db, d.mentions)
    values = blank_values()
    values.update(
        kind=d.kind, date=d.date, title=d.title, raw_text=raw_text, summary_md=draft_summary_md(d),
        follow_ups=[{**f.model_dump(), "done": False} for f in d.follow_ups if f.task.strip()],
        demand_signals=[s.model_dump() for s in d.demand_signals if s.content.strip()],
        selected={"solution": found["solutions"]["matched"], "customer": found["customers"]["matched"],
                  "business": found["businesses"]["matched"], "partner": found["partners"]["matched"]},
        new_candidates={rel: found[rel]["new"] for rel in NEW_TYPES},
        ambiguous={t: _ambiguous_view(db, model, found[rel]["ambiguous"]) for t, (model, rel) in LINK_TYPES.items()},
    )
    return values


@router.post("/draft")
def start_draft(
    request: Request, raw_text: str = Form(""), memo_file: UploadFile | None = None,
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    try:
        text = read_memo(raw_text, memo_file)
    except HTTPException as e:
        return render(request, "activities/new.html", status_code=400, error=e.detail, raw_text=raw_text)
    job = enqueue(db, DRAFT_KIND, {"raw_text": text}, user.id)
    return RedirectResponse(f"/activities/draft/{job.id}", status_code=303)


@router.get("/draft/{job_id}")
def draft_page(job_id: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    job = _draft_job(db, job_id, user)
    raw_text = str((job.input or {}).get("raw_text", ""))
    ctx = dict(job=job, raw_text=raw_text, next=f"/activities/draft/{job.id}", f=None, choices=None, error_kind="")
    if job.status in ("pending", "running"):
        return render(request, "activities/draft.html", position=queue_position(db, job), **ctx)
    draft = None
    if job.status == "done":
        try:
            draft = ActivityDraft.model_validate_json(job.result)
        except ValidationError:
            draft = None
    ctx["choices"] = _choices(db)
    if draft is None:
        values = blank_values()
        values["raw_text"] = raw_text
        ctx["error_kind"] = job.error_kind if job.status == "failed" else "bad_output"
        return render(request, "activities/draft.html", position=0, f=values, **{k: v for k, v in ctx.items() if k != "f"})
    return render(request, "activities/draft.html", position=0, f=draft_values(db, draft, raw_text),
                  **{k: v for k, v in ctx.items() if k != "f"})


@router.post("/draft/{job_id}/retry")
def retry_draft(job_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    job = _draft_job(db, job_id, user)
    if job.status != "failed":
        raise HTTPException(status_code=400, detail="실패한 작업만 다시 시도할 수 있습니다.")
    new = enqueue(db, DRAFT_KIND, dict(job.input or {}), user.id, job.priority)
    return RedirectResponse(f"/activities/draft/{new.id}", status_code=303)


def _form_params(
    kind: str = Form("meeting"), date_: str = Form("", alias="date"), title: str = Form(""),
    raw_text: str = Form(""), memo_file: UploadFile | None = None, summary_md: str = Form(""),
    fu_task: list[str] = Form([]), fu_owner: list[str] = Form([]), fu_due: list[str] = Form([]),
    fu_done: list[str] = Form([]), ds_customer: list[str] = Form([]), ds_content: list[str] = Form([]),
    solution_ids: list[str] = Form([]), customer_ids: list[str] = Form([]),
    business_ids: list[str] = Form([]), partner_ids: list[str] = Form([]),
    new_solutions: list[str] = Form([]), new_customers: list[str] = Form([]), new_partners: list[str] = Form([]),
) -> dict:
    return dict(
        kind=kind, date_=date_, title=title, raw_text=raw_text, memo_file=memo_file, summary_md=summary_md,
        fu_task=fu_task, fu_owner=fu_owner, fu_due=fu_due, fu_done=fu_done,
        ds_customer=ds_customer, ds_content=ds_content, solution_ids=solution_ids, customer_ids=customer_ids,
        business_ids=business_ids, partner_ids=partner_ids,
        new_solutions=new_solutions, new_customers=new_customers, new_partners=new_partners,
    )


@router.post("")
def create(
    request: Request, params: dict = Depends(_form_params),
    user: User = Depends(require_user), db: Session = Depends(get_db),
    mirror: KnowledgeMirror = Depends(get_mirror),
):
    return _save(request, db, user, mirror, None, **params)


@router.get("/{activity_id}")
def detail(activity_id: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    a = get_or_404(db, Activity, activity_id)
    return render(request, "activities/detail.html", activity=a, can_delete=can_delete(user, a))


@router.get("/{activity_id}/edit")
def edit_form(activity_id: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    a = get_or_404(db, Activity, activity_id)
    return _form_page(request, db, form_values(a), a)


@router.post("/{activity_id}")
def update(
    activity_id: int, request: Request, params: dict = Depends(_form_params),
    user: User = Depends(require_user), db: Session = Depends(get_db),
    mirror: KnowledgeMirror = Depends(get_mirror),
):
    return _save(request, db, user, mirror, get_or_404(db, Activity, activity_id), **params)


@router.post("/{activity_id}/delete")
def delete(
    activity_id: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db),
    mirror: KnowledgeMirror = Depends(get_mirror),
):
    a = get_deletable_or_404(db, Activity, activity_id, user)
    linked = _linked(a)
    mirror.remove(a)
    db.delete(a)
    db.commit()
    for obj in linked:
        mirror.sync(obj)
    flash(request, "활동 기록을 삭제했습니다.")
    return RedirectResponse("/activities", status_code=303)


@router.post("/{activity_id}/followups/{index}/toggle")
def toggle_follow_up(
    activity_id: int, index: int, request: Request,
    user: User = Depends(require_user), db: Session = Depends(get_db),
    mirror: KnowledgeMirror = Depends(get_mirror),
):
    a = get_or_404(db, Activity, activity_id)
    items = [dict(f) for f in a.follow_ups or []]
    if not 0 <= index < len(items):
        raise HTTPException(status_code=404, detail="후속 조치를 찾을 수 없습니다.")
    items[index]["done"] = not items[index].get("done", False)
    a.follow_ups = items
    db.commit()
    mirror.sync(a)
    return RedirectResponse(request.headers.get("referer") or f"/activities/{a.id}", status_code=303)
