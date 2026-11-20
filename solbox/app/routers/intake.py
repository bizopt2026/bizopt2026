"""올리기 화면: 파일 여러 개를 한 번에 올리고, 묶음 목록·상세·원본 내려받기."""
from __future__ import annotations

import logging
import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app import intake as intake_lib
from app import labels, transcribe
from app.ai.queue import queue_position
from app.db import get_db
from app.knowledge import KnowledgeMirror, get_mirror
from app.main import render
from app.models import Activity, Business, Customer, Intake, IntakeFile, Partner, Solution, User
from app.routers._common import get_or_404, paginate
from app.routers.home import ai_problem
from app.security import require_user

log = logging.getLogger(__name__)
router = APIRouter(prefix="/intake")

NOT_YOURS = "올린 사람과 관리자만 볼 수 있습니다."
NOTE_TOO_LONG = f"메모는 {intake_lib.MAX_NOTE}자까지 쓸 수 있습니다."
NOTICES = {"not_ready": "이미 저장했거나 저장할 수 없는 상태입니다.", "superseded": intake_lib.SAVE_SUPERSEDED}
CANNOT_DISCARD = "저장 중이거나 저장이 끝난 묶음은 버릴 수 없습니다."
_ASCII_UNSAFE = re.compile(r'[^\x20-\x7e]|["\\%]')


def _is_owner(user: User, intake: Intake) -> bool:
    return user.role == "admin" or intake.created_by_id == user.id


def _ensure_owner(user: User, intake: Intake) -> None:
    if not _is_owner(user, intake):
        raise HTTPException(status_code=403, detail=NOT_YOURS)


async def read_form(request: Request) -> FormData:
    """저장 폼 전체 (칸 이름이 정리안 항목마다 달라 고정된 매개변수로 받을 수 없다)."""
    return await request.form()


def get_intake(db: Session, user: User, intake_id: int) -> Intake:
    intake = get_or_404(db, Intake, intake_id)
    _ensure_owner(user, intake)
    return intake


def _position(db: Session, intake: Intake) -> int:
    return queue_position(db, intake.job) if intake.job is not None else 0


def _result_of(intake: Intake, wanted) -> list[dict]:
    """저장 완료 묶음의 결과 중 종류가 맞는 것 (옛 묶음에는 저장하지 않은 파일 기록이 없다)."""
    if intake.status != "applied":
        return []
    return [e for e in intake.result or [] if wanted(e.get("type"))]


def detail_page(request: Request, user: User, intake: Intake, db: Session, notice: str = "",
                skipped: list[str] | None = None, status_code: int = 200, form: FormData | None = None):
    ready = intake.status == "ready"
    # 엔진이 꺼져 있을 때 올린 음성이 있고 지금은 받아쓸 수 있으면, 다시 정리를 누르라고 안내한다
    can_transcribe = (intake.status not in ("analyzing", "applied", "discarded")
                      and any(f.kind == "audio" and f.read_status == "needs_transcript" for f in intake.files)
                      and transcribe.get(request.app.state.settings).available())
    ctx = {}
    if ready:
        ctx = dict(
            cards=intake_lib.review_cards(db, intake, form),
            choices={
                "solution": list(db.scalars(select(Solution).order_by(Solution.name))),
                "customer": list(db.scalars(select(Customer).order_by(Customer.name))),
                "business": list(db.scalars(select(Business).order_by(Business.name))),
                "partner": list(db.scalars(select(Partner).order_by(Partner.name))),
            },
        )
    return render(
        request, "intake/detail.html", status_code=status_code, user=user, intake=intake, notice=notice, can_transcribe=can_transcribe,
        skipped=skipped or [], intake_files_transcribing=intake_lib.transcribing_file(intake),
        position=_position(db, intake), stale_applying=intake_lib.is_stale_applying(intake), max_note=intake_lib.MAX_NOTE,
        saved=_result_of(intake, lambda t: t not in ("skipped", "unsaved")),
        not_saved=_result_of(intake, lambda t: t == "skipped"),
        unsaved_files=_result_of(intake, lambda t: t == "unsaved"),
        **ctx,
    )


def upload_context(request: Request, db: Session) -> dict:
    """올리기 입력 영역(`_dropzone.html`)이 쓰는 값: 한도, 음성 받아쓰기 가능 여부, AI 사용 불가 안내."""
    settings = request.app.state.settings
    return dict(max_files=intake_lib.MAX_FILES, max_mb=settings.max_upload_mb, max_note=intake_lib.MAX_NOTE,
                transcriber_ready=transcribe.get(settings).available(), ai_problem=ai_problem(db))


@router.get("/new")
def new_page(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    return render(request, "intake/new.html", user=user, error="", note="", **upload_context(request, db))


@router.post("")
def create(
    request: Request, files: list[UploadFile | str] = [], note: str = Form(""),
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    settings = request.app.state.settings

    def again(error: str):
        return render(request, "intake/new.html", status_code=400, user=user, error=error, note=note,
                      **upload_context(request, db))

    if len(note.strip()) > intake_lib.MAX_NOTE:
        return again(NOTE_TOO_LONG)
    try:
        intake, skipped = intake_lib.create_intake(db, settings, user, note, files)
    except ValueError as e:
        return again(str(e))
    try:
        intake_lib.prepare_workdir(settings, intake)
    except Exception:  # noqa: BLE001 - 묶음은 이미 저장되었다. 정리를 시작할 때 작업 폴더를 다시 준비한다
        db.rollback()
        log.exception("올리기 묶음 %s 작업 폴더 준비 실패", intake.id)
    try:
        intake_lib.start_analysis(db, intake, user)
    except Exception:  # noqa: BLE001 - 묶음은 이미 저장되었다. 상세 화면의 "정리 시작" 으로 다시 할 수 있다
        db.rollback()
        log.exception("올리기 묶음 %s 정리 시작 실패", intake.id)
    if skipped:
        return detail_page(request, user, intake, db, skipped=skipped)
    return RedirectResponse(f"/intake/{intake.id}", status_code=303)


@router.get("")
def list_page(request: Request, status: str = "", page: str = "1",
              user: User = Depends(require_user), db: Session = Depends(get_db)):
    stmt = select(Intake)
    if user.role != "admin":
        stmt = stmt.where(Intake.created_by_id == user.id)
    if status in labels.INTAKE_STATUSES:
        stmt = stmt.where(Intake.status == status)
    else:
        status = ""
    items = list(db.scalars(stmt.order_by(Intake.id.desc())))
    owners = {u.id: (u.name or u.username) for u in db.scalars(select(User))} if user.role == "admin" else {}
    return render(request, "intake/list.html", user=user, pg=paginate(items, page), status=status, owners=owners)


@router.get("/files/{file_id}/download")
def download(file_id: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    f = get_or_404(db, IntakeFile, file_id)
    if not _is_owner(user, f.intake):  # 활동 기록에 연결된 원본은 그 기록을 볼 수 있는 로그인 사용자 누구나 내려받는다
        if not (f.target_type == "activity" and f.target_id is not None and db.get(Activity, f.target_id) is not None):
            raise HTTPException(status_code=403, detail=NOT_YOURS)
    path = intake_lib.stored_path(request.app.state.settings, f)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="파일을 찾을 수 없습니다.")
    fallback = _ASCII_UNSAFE.sub("_", f.original_name) or "file"
    return FileResponse(
        path, media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(f.original_name, safe='')}"},
    )


@router.get("/{intake_id}")
def detail(intake_id: int, request: Request, notice: str = "",
           user: User = Depends(require_user), db: Session = Depends(get_db)):
    return detail_page(request, user, get_intake(db, user, intake_id), db, notice=NOTICES.get(notice, ""))


@router.get("/{intake_id}/status")
def status(intake_id: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """정리 중 안내 조각 (HTMX). 끝나면(정리 중이 아니게 되면) 상세 화면으로 보낸다."""
    intake = db.get(Intake, intake_id)
    if intake is None:  # 그 사이 지워졌다. 조회를 멈추고 목록으로 보낸다.
        if user.role != "admin":  # 남의 묶음과 없는 묶음을 구별할 수 없게 같은 응답을 준다
            raise HTTPException(status_code=403, detail=NOT_YOURS)
        return Response("올린 묶음이 없습니다.", headers={"HX-Redirect": "/intake"})
    _ensure_owner(user, intake)
    if intake.status == "analyzing" and intake_lib.finish_failed_analysis(db, intake.id):
        db.refresh(intake)  # 작업이 끝났는데 기록되지 않은 채 남은 묶음을 바로잡았다
    if intake.status != "analyzing":
        return Response("정리가 끝났습니다.", headers={"HX-Redirect": f"/intake/{intake.id}"})
    return render(request, "intake/_status.html", intake=intake, job=intake.job, position=_position(db, intake),
                  intake_files_transcribing=intake_lib.transcribing_file(intake))


@router.post("/{intake_id}/analyze")
def analyze(
    intake_id: int, request: Request, note: str | None = Form(None),
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    """메모를 고치고 다시 정리한다."""
    intake = get_intake(db, user, intake_id)
    if intake.status in ("applied", "discarded"):
        raise HTTPException(status_code=400, detail="이미 끝난 묶음은 다시 정리할 수 없습니다.")
    if intake.status != "analyzing":
        if note is not None:
            if len(note.strip()) > intake_lib.MAX_NOTE:
                return detail_page(request, user, intake, db, notice=NOTE_TOO_LONG, status_code=400)
            intake.note = note.strip()
            db.commit()
        intake_lib.ensure_workdir(request.app.state.settings, intake)  # 준비가 덜 끝난 묶음은 여기서 다시 준비한다
        intake_lib.start_analysis(db, intake, user)
    return RedirectResponse(f"/intake/{intake.id}", status_code=303)


@router.post("/{intake_id}/apply")
def apply(
    intake_id: int, request: Request, form: FormData = Depends(read_form),
    user: User = Depends(require_user), db: Session = Depends(get_db),
    mirror: KnowledgeMirror = Depends(get_mirror),
):
    """정리안을 확인한 대로 저장한다. 한 묶음은 한 번만 저장된다."""
    intake = get_intake(db, user, intake_id)
    try:
        intake_lib.apply(db, request.app.state.settings, mirror, intake, user, form)
    except intake_lib.Superseded:
        return RedirectResponse(f"/intake/{intake.id}?notice=superseded", status_code=303)
    except intake_lib.NotReady:
        return RedirectResponse(f"/intake/{intake.id}?notice=not_ready", status_code=303)
    except ValueError as e:  # 입력 오류: 입력값을 그대로 둔 채 화면을 다시 보여 준다
        return detail_page(request, user, intake, db, notice=str(e), status_code=400, form=form)
    except intake_lib.ApplyFailed:  # 오류 문구는 묶음에 적혀 확인 화면에 보인다
        return detail_page(request, user, intake, db, status_code=500, form=form)
    return RedirectResponse(f"/intake/{intake.id}", status_code=303)


@router.post("/{intake_id}/discard")
def discard(intake_id: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """묶음을 버린다. 올린 원본은 지우지 않는다."""
    intake = get_intake(db, user, intake_id)
    if not intake_lib.discard(db, request.app.state.settings, intake):
        raise HTTPException(status_code=400, detail=CANNOT_DISCARD)
    return RedirectResponse(f"/intake/{intake.id}", status_code=303)


@router.post("/{intake_id}/recheck")
def recheck(intake_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """오래 저장 중으로 멈춘 묶음을 확인 대기로 되돌린다 (아직 저장 중인 묶음은 그대로 둔다)."""
    intake = get_intake(db, user, intake_id)
    intake_lib.recheck_applying(db, intake)
    return RedirectResponse(f"/intake/{intake.id}", status_code=303)
