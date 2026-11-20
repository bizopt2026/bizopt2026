"""솔루션 자료 파일: 여러 개 올리기, 내려받기, 종류 변경, 삭제."""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.flash import flash
from app import labels
from app.db import get_db
from app.extract import extract_text
from app.knowledge import KnowledgeMirror, file_markdown, get_mirror
from app.models import Solution, SolutionFile, User
from app.routers._common import get_or_404
from app.routers.solutions import detail_page
from app.security import ensure_can_delete, require_user

router = APIRouter()
BAD_KIND = "알 수 없는 자료 종류입니다."
CHUNK = 1024 * 1024
_EXT = re.compile(r"\.[a-z0-9]{1,10}")


def _upload_dir(request: Request, solution_id: int) -> Path:
    return request.app.state.settings.uploads_dir / "solutions" / str(solution_id)


def _stored_path(request: Request, f: SolutionFile) -> Path:
    return _upload_dir(request, f.solution_id) / f.stored_name


def _clean_name(filename: str) -> str:
    """브라우저가 경로째 보낸 이름에서 파일명만 남긴다. 브라우저는 따옴표를 %22 로 보낸다."""
    filename = filename.replace("%22", '"').replace("%0D", "").replace("%0A", "")
    return Path(filename.replace("\\", "/")).name


def _extension(name: str) -> str:
    ext = Path(name).suffix.lower()
    return ext if _EXT.fullmatch(ext) else ""


def _save_upload(db: Session, mirror: KnowledgeMirror, request: Request, solution: Solution,
                 upload: UploadFile, name: str, kind: str, user: User, limit: int) -> bool:
    """파일 하나를 저장한다. 크기 제한을 넘으면 아무것도 남기지 않고 False."""
    f = SolutionFile(solution_id=solution.id, original_name=name, kind=kind, created_by_id=user.id)
    db.add(f)
    db.flush()  # file_id 를 얻는다
    f.stored_name = f"{f.id}{_extension(name)}"
    path = _stored_path(request, f)
    path.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    try:
        with path.open("wb") as out:
            while chunk := upload.file.read(CHUNK):
                size += len(chunk)
                if size > limit:
                    break
                out.write(chunk)
        if size > limit:
            path.unlink(missing_ok=True)
            db.rollback()
            return False
        f.size = size
        result = extract_text(path)
        f.extract_status = result.status
        db.commit()
    except BaseException:
        path.unlink(missing_ok=True)
        db.rollback()
        raise
    if result.status == "ok":
        mirror.write_file_text(f, file_markdown(f, result.text))
    return True


@router.post("/solutions/{solution_id}/files")
def upload_files(
    solution_id: int, request: Request,
    files: list[UploadFile | str] = [], kind: str = Form("related"),
    user: User = Depends(require_user), db: Session = Depends(get_db),
    mirror: KnowledgeMirror = Depends(get_mirror),
):
    solution = get_or_404(db, Solution, solution_id)
    if kind not in labels.FILE_KINDS:
        raise HTTPException(status_code=400, detail=BAD_KIND)
    max_mb = request.app.state.settings.max_upload_mb
    skipped: list[str] = []
    for upload in files:
        if isinstance(upload, str):  # 파일을 고르지 않고 보낸 빈 칸은 문자열로 들어온다
            continue
        name = _clean_name(upload.filename or "")
        if not name:
            continue
        if not _save_upload(db, mirror, request, solution, upload, name, kind, user, max_mb * 1024 * 1024):
            skipped.append(name)
    db.refresh(solution)
    mirror.sync(solution)
    if skipped:
        notice = f"크기 제한({max_mb}MB)을 넘어 건너뛴 파일: {', '.join(skipped)}"
        return detail_page(request, db, user, solution, notice=notice)
    flash(request, "파일을 올렸습니다.")
    return RedirectResponse(f"/solutions/{solution.id}", status_code=303)


@router.post("/files/{file_id}/kind")
def change_kind(
    file_id: int, request: Request, kind: str = Form(""),
    user: User = Depends(require_user), db: Session = Depends(get_db),
    mirror: KnowledgeMirror = Depends(get_mirror),
):
    f = get_or_404(db, SolutionFile, file_id)
    if kind not in labels.FILE_KINDS:
        raise HTTPException(status_code=400, detail=BAD_KIND)
    f.kind = kind
    db.commit()
    if f.extract_status == "ok":  # 사본 첫 줄에 종류 이름이 들어 있다. 다시 읽기에 실패하면 기존 사본을 그대로 둔다
        result = extract_text(_stored_path(request, f))
        if result.status == "ok":
            mirror.write_file_text(f, file_markdown(f, result.text))
    mirror.sync(f.solution)
    return RedirectResponse(f"/solutions/{f.solution_id}", status_code=303)


@router.get("/files/{file_id}/download")
def download(
    file_id: int, request: Request,
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    f = get_or_404(db, SolutionFile, file_id)
    path = _stored_path(request, f)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="파일을 찾을 수 없습니다.")
    return FileResponse(
        path, media_type="application/octet-stream",
        headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(f.original_name)},
    )


@router.post("/files/{file_id}/delete")
def delete_file(
    file_id: int, request: Request,
    user: User = Depends(require_user), db: Session = Depends(get_db),
    mirror: KnowledgeMirror = Depends(get_mirror),
):
    f = get_or_404(db, SolutionFile, file_id)
    ensure_can_delete(user, f)
    solution = f.solution
    mirror.remove_file_text(f)
    _stored_path(request, f).unlink(missing_ok=True)
    db.delete(f)
    db.commit()
    db.refresh(solution)
    mirror.sync(solution)
    flash(request, "파일을 삭제했습니다.")
    return RedirectResponse(f"/solutions/{solution.id}", status_code=303)
