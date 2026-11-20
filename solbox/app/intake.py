"""올리기 묶음: 여러 파일을 한 번에 받아 저장하고, 묶음별 작업 폴더(글 사본·줄인 사진)를 준비한다."""
from __future__ import annotations

import logging
import re
import secrets
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import UploadFile
from PIL import Image, ImageOps
from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session, object_session

from app import labels, transcribe
from app.ai.tasks import ActivityDraft, draft_summary_md, match_mentions, normalize_draft, normalize_name
from app.config import Settings
from app.extract import ExtractResult, extract_text
from app.knowledge import KnowledgeMirror, file_markdown, safe_name
from app.models import AIJob, Activity, Business, Customer, Intake, IntakeFile, Partner, Solution, User

if TYPE_CHECKING:
    from app.ai.intake_tasks import IntakePlanOut

log = logging.getLogger(__name__)

CHUNK = 1024 * 1024
MAX_FILES = 20
MAX_NOTE = 500
MAX_IMAGE_SIDE = 2000
MAX_IMAGE_PIXELS = 40_000_000  # 이보다 큰 사진은 열지 않고 읽지 못한 것으로 둔다
MAX_COPY_NAME_BYTES = 150  # 작업 폴더 사본 이름의 UTF-8 바이트 상한 (파일 시스템 이름 길이 제한 대비)
MAX_RAW_TEXT = 200_000
MAX_TAGS = 5
NO_READABLE = "읽을 수 있는 파일이 없습니다."
NOT_PLACED = "정리 대상에 포함되지 않음"
NO_FILES = "올릴 파일을 선택하세요."
FILE_LIST_NAME = "파일목록.md"
HINT_MAX_CHARS = 200  # 받아쓰기 힌트(초기 프롬프트) 전체 글자 수: 모델이 읽는 창이 작다
HINT_NOTE_CHARS = 80
TRANSCRIPT_SUFFIX = ".transcript.txt"
TRANSCRIPT_HEADER = "#model: "
TRANSCRIPT_LANGUAGE = "#language: "
AUDIO_LISTED_KIND = "음성(받아쓰기)"

_EXT = re.compile(r"\.[a-z0-9]{1,10}")
_KINDS = {
    "document": {".pptx", ".pdf", ".docx", ".xlsx", ".txt", ".md", ".csv"},
    "image": {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"},
    "audio": {".mp3", ".m4a", ".wav", ".aac", ".ogg", ".flac", ".wma"},
}
_IMAGE_ERRORS = (OSError, SyntaxError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning)
_CONTROL = re.compile(r"[\x00-\x1f\x7f\u0085\u2028\u2029]+")
_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")  # 폭 없는 글자와 글자 방향 제어 (글을 가리거나 뒤집는 데 쓰인다)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"\+?\d[\d\-\s().]{5,}\d")


def clean_name(filename: str) -> str:
    """브라우저가 경로째 보낸 이름에서 파일명만 남긴다. 브라우저는 따옴표를 %22 로 보낸다."""
    filename = filename.replace("%22", '"').replace("%0D", "").replace("%0A", "")
    return Path(filename.replace("\\", "/")).name


def extension(name: str) -> str:
    """저장 이름에 붙일 확장자: 소문자 `[a-z0-9]{1,10}` 만, 아니면 빈 문자열."""
    ext = Path(name).suffix.lower()
    return ext if _EXT.fullmatch(ext) else ""


def file_kind(name: str) -> str:
    ext = Path(name).suffix.lower()
    for kind, exts in _KINDS.items():
        if ext in exts:
            return kind
    return "other"


def write_upload(upload: UploadFile, path: Path, limit: int) -> int | None:
    """업로드를 조각 단위로 `path` 에 쓴다. 쓴 바이트 수를, 크기 제한(`limit`)을 넘으면 파일을 지우고 None 을 돌려준다.
    쓰다 실패하면 부분 파일을 지우고 예외를 그대로 올린다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    try:
        with path.open("wb") as out:
            while chunk := upload.file.read(CHUNK):
                size += len(chunk)
                if size > limit:
                    break
                out.write(chunk)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    if size > limit:
        path.unlink(missing_ok=True)
        return None
    return size


def intake_upload_dir(settings: Settings, intake_id: int) -> Path:
    return settings.uploads_dir / "intake" / str(intake_id)


def stored_path(settings: Settings, f: IntakeFile) -> Path:
    return intake_upload_dir(settings, f.intake_id) / f.stored_name


def create_intake(db: Session, settings: Settings, user: User, note: str,
                  uploads: list[UploadFile | str]) -> tuple[Intake, list[str]]:
    """묶음을 만들고 파일을 저장한다. (묶음, 받지 못한 파일 안내 목록). 받은 파일이 없으면 ValueError."""
    limit_mb = settings.max_upload_mb
    intake = Intake(note=note.strip(), created_by_id=user.id)
    db.add(intake)
    db.flush()
    skipped: list[str] = []
    accepted = 0
    try:
        for upload in uploads:
            if isinstance(upload, str):  # 파일을 고르지 않고 보낸 빈 칸은 문자열로 들어온다
                continue
            name = clean_name(upload.filename or "")
            if not name:
                continue
            if accepted >= MAX_FILES:
                skipped.append(f"{name} (한 번에 {MAX_FILES}개까지)")
                continue
            f = IntakeFile(intake_id=intake.id, original_name=name, kind=file_kind(name))
            db.add(f)
            db.flush()  # file_id 를 얻는다
            f.stored_name = f"{f.id}{extension(name)}"
            size = write_upload(upload, stored_path(settings, f), limit_mb * 1024 * 1024)
            if size is None or size == 0:
                if size == 0:
                    stored_path(settings, f).unlink(missing_ok=True)
                db.delete(f)
                db.flush()
                skipped.append(f"{name} ({'빈 파일' if size == 0 else f'크기 제한 {limit_mb}MB 초과'})")
                continue
            f.size = size
            accepted += 1
        if not accepted:
            raise ValueError(NO_FILES)
        db.commit()
    except BaseException:
        db.rollback()
        shutil.rmtree(intake_upload_dir(settings, intake.id), ignore_errors=True)
        raise
    return intake, skipped


def file_number(intake: Intake, f: IntakeFile) -> int:
    """묶음 안에서 파일의 1부터의 순번 (id 순). 정리안의 "파일 번호"다."""
    return [x.id for x in sorted(intake.files, key=lambda x: x.id)].index(f.id) + 1


def work_dir(settings: Settings, intake: Intake) -> Path:
    return settings.intake_work_dir / str(intake.id)


def _flat(name: str) -> str:
    """줄바꿈·탭 같은 제어 문자를 공백 하나로 줄인다 (글 사본의 `# 이름` 줄과 파일목록 표가 깨지지 않게)."""
    return _CONTROL.sub(" ", _INVISIBLE.sub("", name)).strip()


def _copy_base(number: int, name: str, reserve: int = 5) -> str:
    """사본 이름의 앞부분 `<번호>-<안전한 이름>`. 뒤에 붙을 확장자 `reserve` 바이트를 남기고 전체를 UTF-8 150바이트 이하로 줄인다."""
    prefix = f"{number}-"
    room = MAX_COPY_NAME_BYTES - len(prefix.encode("utf-8")) - reserve
    safe = safe_name(name).encode("utf-8")[:max(room, 1)].decode("utf-8", errors="ignore").strip(" .")
    return prefix + (safe or "file")


def doc_copy_name(number: int, f: IntakeFile) -> str:
    """문서 글 사본의 이름."""
    return _copy_base(number, f.original_name) + ".md"


def _shrink_image(src: Path, dest_base: Path) -> Path | None:
    """사진을 열어 방향을 바로잡고 긴 변 2,000px 이하로 줄여 저장한다. 저장한 경로, 열지 못하거나 너무 크면 None."""
    try:
        with Image.open(src) as opened:
            width, height = opened.size  # 그림을 풀기 전에 PIL 이 알려 주는 크기
            if width * height > MAX_IMAGE_PIXELS:
                return None
            img = ImageOps.exif_transpose(opened)
            img.load()
        has_alpha = img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info
        if has_alpha:
            img = img.convert("RGBA")
            dest = dest_base.with_name(dest_base.name + ".png")
        else:
            img = img.convert("RGB")
            dest = dest_base.with_name(dest_base.name + ".jpg")
        img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
        try:
            img.save(dest, "PNG" if has_alpha else "JPEG", **({} if has_alpha else {"quality": 88}))
        except BaseException:
            dest.unlink(missing_ok=True)  # 쓰다 만 사본을 남기지 않는다
            raise
        return dest
    except _IMAGE_ERRORS:
        return None


# ---- 음성 받아쓰기 (작업 폴더 사본·저장해 둔 글·힌트) ----

def transcript_cache_path(settings: Settings, f: IntakeFile) -> Path:
    """받아쓴 글을 원본 옆에 저장해 두는 곳. 다시 정리·작업 폴더 재생성 때 다시 받아쓰지 않으려고 쓴다."""
    return stored_path(settings, f).with_name(f.stored_name + TRANSCRIPT_SUFFIX)


def read_transcript_cache(settings: Settings, f: IntakeFile, model: str, language: str = "") -> str | None:
    """저장해 둔 받아쓴 글. 없거나 다른 모델·다른 언어 설정으로 받아쓴 것이면 None. (`f` 는 `stored_name` 등을 가진 어떤 객체든 된다.)"""
    try:
        head, lang, body = transcript_cache_path(settings, f).read_text(encoding="utf-8").split("\n", 2)
    except (OSError, ValueError):
        return None
    if head != TRANSCRIPT_HEADER + model or lang != TRANSCRIPT_LANGUAGE + language:
        return None
    return body.strip() or None


def write_transcript_cache(settings: Settings, f: IntakeFile, model: str, text: str, language: str = "") -> None:
    path = transcript_cache_path(settings, f)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(f"{TRANSCRIPT_HEADER}{model}\n{TRANSCRIPT_LANGUAGE}{language}\n{text}\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        tmp.unlink(missing_ok=True)
        log.warning("받아쓴 글을 저장해 두지 못했습니다 (파일 %s)", f.id, exc_info=True)


def write_audio_copy(folder: Path, number: int, f: IntakeFile, text: str) -> str:
    """받아쓴 글을 문서처럼 글 사본(.md)으로 쓴다. 사본 이름."""
    name = doc_copy_name(number, f)
    try:
        (folder / name).write_text(f"# {_flat(f.original_name)}\n\n{text}\n", encoding="utf-8")
    except OSError:
        (folder / name).unlink(missing_ok=True)
        raise
    return name


def _prepare_audio(settings: Settings, f: IntakeFile, number: int, folder: Path, transcriber: transcribe.Transcriber) -> str:
    """음성: 요청 안에서는 받아쓰지 않는다. 엔진이 없으면 받아쓰기 필요, 있으면 받아쓰기 대기(정리 작업이 받아쓴다).
    이미 받아쓴 글이 저장돼 있으면 그대로 써서 바로 읽음으로 둔다."""
    f.read_error, f.progress = None, None
    if not transcriber.available():
        f.read_status = "needs_transcript"
        return ""
    cached = read_transcript_cache(settings, f, transcriber.model_name(), transcriber.language_name())
    if cached:
        f.read_status = "ok"
        return write_audio_copy(folder, number, f, cached)
    f.read_status = "pending_transcript"
    return ""


def registered_names(db: Session) -> list[str]:
    """등록된 솔루션·고객·파트너·사업 이름, 최근 것 먼저."""
    rows: list[tuple[datetime, int, str]] = []
    for model in (Solution, Customer, Partner, Business):
        rows.extend((o.created_at or datetime.min, o.id, o.name) for o in db.scalars(select(model)))
    rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
    return [r[2] for r in rows]


def build_hints(names: list[str], note: str, limit: int = HINT_MAX_CHARS) -> str:
    """받아쓰기 힌트: 등록된 이름(최근 먼저, 겹치는 것 뺌, 이름은 중간에서 자르지 않음) 뒤에 묶음 메모. 전체 `limit` 글자 이하."""
    note = _flat(note or "")[:HINT_NOTE_CHARS].strip()
    room = limit - (len(note) + 1 if note else 0)
    picked: list[str] = []
    seen: set[str] = set()
    used = 0
    for raw in names:
        name = _flat(raw or "")
        key = normalize_name(name)
        if len(key) < 2 or key in seen:
            continue
        cost = len(name) + (2 if picked else 0)
        if used + cost > room:
            continue
        picked.append(name)
        seen.add(key)
        used += cost
    return " ".join(x for x in (", ".join(picked), note) if x)


def transcribing_file(intake: Intake) -> IntakeFile | None:
    """지금 받아쓰고 있는(받아쓰기 대기 중 가장 앞) 음성 파일."""
    pending = [f for f in sorted(intake.files, key=lambda x: x.id) if f.kind == "audio" and f.read_status == "pending_transcript"]
    return pending[0] if pending else None


def has_audio_to_transcribe(db: Session, intake_id: int) -> bool:
    return db.scalar(
        select(func.count()).select_from(IntakeFile).where(
            IntakeFile.intake_id == intake_id, IntakeFile.kind == "audio",
            IntakeFile.read_status.in_(("pending_transcript", "failed")),
        )
    ) > 0


def _prepare_file(settings: Settings, f: IntakeFile, number: int, src: Path, folder: Path, transcriber: transcribe.Transcriber) -> str:
    """파일 하나의 작업 폴더 사본을 만들고 읽기 상태를 정한다. 사본 이름(없으면 빈 글)."""
    copy_name = ""
    if f.kind == "document":
        result = extract_text(src)
        f.read_status = result.status
        if result.status == "ok":
            name = doc_copy_name(number, f)
            try:
                (folder / name).write_text(f"# {_flat(f.original_name)}\n\n{result.text}\n", encoding="utf-8")
            except OSError:
                (folder / name).unlink(missing_ok=True)  # 쓰다 만 사본을 AI 가 읽지 않게 한다
                raise
            copy_name = name
    elif f.kind == "image":
        dest = _shrink_image(src, folder / _copy_base(number, f.original_name))
        f.read_status = "ok" if dest else "failed"
        copy_name = dest.name if dest else ""
    elif f.kind == "audio":
        copy_name = _prepare_audio(settings, f, number, folder, transcriber)
    else:
        f.read_status = "unsupported"
    return copy_name


def prepare_workdir(settings: Settings, intake: Intake) -> None:
    """`intake_work/<id>/` 를 비우고 다시 만든다: 문서는 글 사본(.md), 사진은 줄인 사본, 파일목록.md.
    한 파일의 사본을 만들지 못해도(어떤 오류든) 그 파일만 읽지 못함으로 두고 나머지를 계속한다."""
    folder = work_dir(settings, intake)
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    transcriber = transcribe.get(settings)
    for f in sorted(intake.files, key=lambda x: x.id):
        number = file_number(intake, f)
        try:
            _prepare_file(settings, f, number, stored_path(settings, f), folder, transcriber)
        except Exception:  # noqa: BLE001 - 파일 하나가 깨져도 묶음 전체의 준비를 멈추지 않는다
            log.exception("올리기 묶음 %s 의 파일 %s 사본을 만들지 못했습니다", intake.id, f.id)
            f.read_status = "failed"
    write_file_list(settings, intake)
    session = object_session(intake)
    if session is not None:
        session.commit()


def _existing_copy(folder: Path, number: int, f: IntakeFile) -> str:
    """작업 폴더에 실제로 있는 사본의 이름 (없으면 빈 글)."""
    if f.read_status != "ok":
        return ""
    if f.kind in ("document", "audio"):
        name = doc_copy_name(number, f)
        return name if (folder / name).is_file() else ""
    if f.kind == "image":
        base = _copy_base(number, f.original_name)
        for ext in (".jpg", ".png"):
            if (folder / (base + ext)).is_file():
                return base + ext
    return ""


def write_file_list(settings: Settings, intake: Intake) -> None:
    """`파일목록.md` 를 지금의 읽기 상태와 작업 폴더 사본대로 다시 쓴다."""
    folder = work_dir(settings, intake)
    lines = ["# 파일 목록", "", "| 번호 | 원래 이름 | 종류 | 읽기 상태 | 사본 파일 |", "|---|---|---|---|---|"]
    for f in sorted(intake.files, key=lambda x: x.id):
        number = file_number(intake, f)
        name = _flat(f.original_name).replace("|", "\\|")
        kind = AUDIO_LISTED_KIND if f.kind == "audio" and f.read_status == "ok" else labels.INTAKE_FILE_KINDS.get(f.kind, f.kind)
        lines.append(
            f"| {number} | {name} | {kind} | "
            f"{labels.READ_STATUSES.get(f.read_status, f.read_status)} | {_existing_copy(folder, number, f) or '(없음)'} |"
        )
    (folder / FILE_LIST_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")


def remove_workdir(settings: Settings, intake: Intake) -> None:
    shutil.rmtree(work_dir(settings, intake), ignore_errors=True)


def needs_prepare(settings: Settings, intake: Intake) -> bool:
    """작업 폴더 준비가 끝나지 않았거나(읽기 상태가 비었거나 읽지 못함) 파일목록이 없으면 True."""
    if any(f.read_status in ("", "failed") for f in intake.files):
        return True
    if intake.status not in ("applied", "discarded") and any(f.kind == "audio" and f.read_status == "needs_transcript" for f in intake.files):
        if transcribe.get(settings).available():  # 엔진을 나중에 켰다: 받아쓰기 필요였던 음성을 대기로 바꾼다
            return True
    return not (work_dir(settings, intake) / FILE_LIST_NAME).is_file()


def ensure_workdir(settings: Settings, intake: Intake) -> None:
    """정리를 시작하기 전에 필요하면 작업 폴더를 다시 준비한다 (일시적인 실패가 다음 시도에서 낫도록).
    준비 자체가 실패해도 올리지 못하는 것은 아니므로 기록만 남긴다 (읽을 수 있는 파일이 없으면 정리 시작이 실패로 둔다)."""
    if not needs_prepare(settings, intake):
        return
    try:
        prepare_workdir(settings, intake)
    except Exception:  # noqa: BLE001
        session = object_session(intake)
        if session is not None:
            session.rollback()
        log.exception("올리기 묶음 %s 작업 폴더 준비 실패", intake.id)


# ---- 정리안 검증 ----

def _ordered(numbers: list[int], valid: set[int]) -> list[int]:
    """파일 번호에서 이 묶음의 읽은 파일만, 중복 없이 처음 나온 순서로 남긴다."""
    seen: list[int] = []
    for n in numbers:
        if n in valid and n not in seen:
            seen.append(n)
    return seen


def _merge_into(base: dict, other: dict, fields: tuple[str, ...]) -> None:
    """같은 이름의 항목을 하나로 합친다: 파일은 합치고, 비어 있는 칸은 뒤 항목의 값으로 채운다."""
    base["files"] = base["files"] + [n for n in other["files"] if n not in base["files"]]
    for field in fields:
        if not base.get(field) and other.get(field):
            base[field] = other[field]


def _raw_text(settings: Settings | None, intake: Intake, number: int, f: IntakeFile) -> str | None:
    """문서 파일의 추출 글 전체 (작업 폴더 사본에서 `# 이름` 머리줄을 뺀 것). 읽지 못하면 None."""
    if settings is None or f.kind not in ("document", "audio"):
        return None
    path = work_dir(settings, intake) / doc_copy_name(number, f)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    head, _, body = text.partition("\n")
    body = (body if head.startswith("# ") else text).strip()
    return body[:MAX_RAW_TEXT] or None


CONTACT_FIELDS = ("person", "role", "phone", "email", "address", "note")
USABLE_CONTACT_FIELDS = ("person", "phone", "email", "address")  # 이 중 하나는 있어야 덧붙일 연락처다
CONTACT_NOTE_PLAN_CAP = 60  # 정리안에서 연락처 메모는 이 글자 수까지만 (줄 하나가 너무 길어지지 않게)
CONTACT_CAPS = {"person": 100, "role": 100, "phone": 100, "email": 100, "address": 200, "note": 200}  # 칸마다 글자 수 제한


def _digits_only(text: str) -> str:
    return re.sub(r"\D", "", text or "")


def split_contact(text: str) -> tuple[str, str, str]:
    """한 줄 연락처 글에서 (전화, 이메일, 나머지)를 나눈다. 새 파트너·고객 항목을 연락처 덧붙이기로 바꿀 때 쓴다."""
    text = str(text or "")
    email = _EMAIL.search(text)
    rest = text.replace(email.group(0), " ") if email else text
    phone = _PHONE.search(rest)
    rest = rest.replace(phone.group(0), " ") if phone else rest
    rest = re.sub(r"\s+", " ", rest).strip(" \t·,/;|-")
    return (phone.group(0).strip() if phone else ""), (email.group(0) if email else ""), rest


def clean_plan(db: Session, intake: Intake, out: "IntakePlanOut", today: date, settings: Settings | None = None) -> dict:
    """AI 정리안을 저장용으로 검증·보정한다: `{"items": [...]}`.

    항목 순서는 파트너, 고객, 새 솔루션, 솔루션에 추가, 연락처 추가, 활동, 정리하지 못한 파일. `settings` 가 있으면 활동의 원문을
    작업 폴더의 글 사본에서 읽는다 (없으면 정리본을 원문으로 쓴다)."""
    by_number = {file_number(intake, f): f for f in intake.files}
    readable = {n for n, f in by_number.items() if f.read_status == "ok"}
    used: set[int] = set()

    def files_of(numbers: list[int]) -> list[int]:
        return _ordered(numbers, readable)

    solutions = {normalize_name(s.name): s.id for s in db.scalars(select(Solution).order_by(Solution.id)) if normalize_name(s.name)}
    solution_ids = set(db.scalars(select(Solution.id)))
    solution_name_of = {s.id: s.name for s in db.scalars(select(Solution))}
    customer_rows = list(db.scalars(select(Customer).order_by(Customer.id)))
    customers = {normalize_name(c.name): c.id for c in customer_rows if normalize_name(c.name)}
    target_name_of = {"partner": {p.id: p.name for p in db.scalars(select(Partner))},
                      "customer": {c.id: c.name for c in customer_rows}}
    contacts: dict[tuple, dict] = {}

    def add_contact(target_type: str, target_id: int, numbers: list[int], reason: str, **fields: str) -> None:
        item = {"kind": "add_contact", "reason": reason.strip(), "files": files_of(numbers), "target_type": target_type,
                "target_id": target_id, "target_name": target_name_of[target_type][target_id],
                **{f: _flat(fields.get(f, ""))[:CONTACT_CAPS[f]].strip() for f in CONTACT_FIELDS}}
        if not any(item[f] for f in USABLE_CONTACT_FIELDS):
            return
        if len(item["note"]) > CONTACT_NOTE_PLAN_CAP:
            item["note"] = item["note"][:CONTACT_NOTE_PLAN_CAP - 1].rstrip() + "…"
        same = (target_type, target_id, normalize_name(item["person"]), _digits_only(item["phone"]), item["email"].lower())
        if same in contacts:
            _merge_into(contacts[same], item, ("role", "address", "note", "reason"))
        else:
            contacts[same] = item
    partners = {normalize_name(p.name): p.id for p in db.scalars(select(Partner).order_by(Partner.id)) if normalize_name(p.name)}

    new_partners: dict[str, dict] = {}
    for p in out.partners:
        key = normalize_name(p.name)
        if not key:
            continue
        if key in partners:  # 이미 등록된 파트너: 연락처가 있으면 연락처 덧붙이기로 바꾼다 (없으면 버린다)
            if p.contact.strip():
                phone, email, rest = split_contact(p.contact)
                add_contact("partner", partners[key], p.files, p.reason, phone=phone, email=email, note=rest)
            continue
        item = {"kind": "new_partner", "reason": p.reason.strip(), "files": files_of(p.files), "name": p.name.strip(),
                "contact": p.contact.strip(), "description": p.description.strip(), "memo": p.memo.strip()}
        if key in new_partners:
            _merge_into(new_partners[key], item, ("contact", "description", "memo", "reason"))
        else:
            new_partners[key] = item

    new_customers: dict[str, dict] = {}
    for c in out.customers:
        key = normalize_name(c.name)
        if not key:
            continue
        if key in customers:
            if c.contact.strip():
                phone, email, rest = split_contact(c.contact)
                add_contact("customer", customers[key], c.files, c.reason, phone=phone, email=email, note=rest)
            continue
        item = {"kind": "new_customer", "reason": c.reason.strip(), "files": files_of(c.files), "name": c.name.strip(),
                "industry": c.industry.strip(), "contact": c.contact.strip(), "memo": c.memo.strip()}
        if key in new_customers:
            _merge_into(new_customers[key], item, ("industry", "contact", "memo", "reason"))
        else:
            new_customers[key] = item

    new_solutions: dict[str, dict] = {}
    additions: dict[int, dict] = {}

    def add_addition(solution_id: int, numbers: list[int], note: str, reason: str) -> None:
        item = {"kind": "add_to_solution", "reason": reason.strip(), "files": files_of(numbers),
                "solution_id": solution_id, "solution_name": solution_name_of.get(solution_id, ""), "note": note.strip()}
        if not item.get("files", []):
            return
        if solution_id in additions:
            _merge_into(additions[solution_id], item, ("note", "reason"))
        else:
            additions[solution_id] = item

    for s in out.solutions:
        key = normalize_name(s.name)
        if not key:
            continue
        if key in solutions:  # 이미 등록된 솔루션이면 새로 만들지 않고 자료만 더한다
            add_addition(solutions[key], s.files, s.summary, s.reason)
            continue
        partner_key = normalize_name(s.partner)
        item = {
            "kind": "new_solution", "reason": s.reason.strip(), "files": files_of(s.files), "name": s.name.strip(),
            "summary": s.summary.strip(), "description": s.description.strip(),
            "tags": list(dict.fromkeys(t.strip() for t in s.tags if t.strip()))[:MAX_TAGS],
            "partner_id": partners.get(partner_key) if partner_key else None,
            "partner_new": s.partner.strip() if partner_key and partner_key not in partners else "",
        }
        if key in new_solutions:
            _merge_into(new_solutions[key], item, ("summary", "description", "reason", "partner_id", "partner_new"))
            merged = new_solutions[key]
            merged["tags"] = list(dict.fromkeys(merged["tags"] + item["tags"]))[:MAX_TAGS]
        else:
            new_solutions[key] = item

    for a in out.additions:
        if a.solution_id in solution_ids:
            add_addition(a.solution_id, a.files, a.note, a.reason)

    for c in out.contacts:
        if c.target_type in target_name_of and c.target_id in target_name_of[c.target_type]:
            add_contact(c.target_type, c.target_id, c.files, c.reason, person=c.person, role=c.role, phone=c.phone,
                        email=c.email, address=c.address, note=c.note)

    activities: list[dict] = []
    for act in out.activities:
        if act.draft is None:  # 쓸 수 없는 초안은 그 항목만 버린다
            continue
        draft = normalize_draft(act.draft, today)
        numbers = files_of(act.files)
        raw = None
        if act.raw_from in readable:
            raw = _raw_text(settings, intake, act.raw_from, by_number[act.raw_from])
            if raw is not None and act.raw_from not in numbers:
                numbers.append(act.raw_from)
        summary = draft_summary_md(draft)
        matches = match_mentions(db, draft.mentions)
        activities.append({
            "kind": "activity", "reason": act.reason.strip(), "files": numbers,
            "draft": draft.model_dump(), "summary_md": summary, "raw_text": raw or summary,
            "matched": {rel: m["matched"] for rel, m in matches.items()},
            "new": {rel: m["new"] for rel, m in matches.items()},
            "ambiguous": {rel: m["ambiguous"] for rel, m in matches.items()},
        })

    groups = [list(new_partners.values()), list(new_customers.values()), list(new_solutions.values()),
              list(additions.values()), list(contacts.values()), activities]
    prefixes = ["p", "c", "s", "a", "ct", "act"]
    items: list[dict] = []
    for prefix, group in zip(prefixes, groups):
        for i, item in enumerate(group, 1):
            items.append({"kind": item["kind"], "key": f"{prefix}{i}", **{k: v for k, v in item.items() if k != "kind"}})
            used.update(item.get("files", []))

    flag_similar_follow_ups(items)
    unused = [n for n in sorted(readable) if n not in used]
    unread = [n for n in sorted(by_number) if n not in readable]
    if unused or unread:
        reasons = []
        if unused:
            given = [u.reason.strip() for u in out.unplaced if u.reason.strip() and set(files_of(u.files)) & set(unused)]
            reasons.append(" / ".join(dict.fromkeys(given)) or NOT_PLACED)
        if unread:
            states = dict.fromkeys(labels.READ_STATUSES.get(by_number[n].read_status, by_number[n].read_status) for n in unread)
            reasons.append("읽을 수 없는 파일 (" + ", ".join(states) + ")")
        items.append({"kind": "unplaced", "key": "u1", "reason": " · ".join(reasons), "files": unused + unread})
    return {"items": items}


_PARTICLES = "을를은는이가의에로과와도"
_PUNCT = re.compile(r"[^\w\s]")


def _task_text(task: str) -> str:
    return " ".join(_PUNCT.sub(" ", str(task or "").lower()).split())


def _task_tokens(task: str) -> list[str]:
    """할 일 글의 낱말 (끝의 조사 한 글자는 뗀다)."""
    return [t[:-1] if len(t) >= 3 and t[-1] in _PARTICLES else t for t in _task_text(task).split()]


def _same_token(a: str, b: str) -> bool:
    return a == b or (min(len(a), len(b)) >= 3 and (a.startswith(b) or b.startswith(a)))


def similar_tasks(a: dict, b: dict) -> bool:
    """두 후속 조치가 같은 할 일로 보이는가: 정리한 글이 같거나, 기한이 같고 낱말이 대부분 겹친다."""
    ta, tb = _task_text(a.get("task", "")), _task_text(b.get("task", ""))
    if not ta or not tb:
        return False
    if ta == tb:
        return True
    due_a, due_b = str(a.get("due") or "").strip(), str(b.get("due") or "").strip()
    if not due_a or due_a != due_b:
        return False
    wa, wb = _task_tokens(ta), _task_tokens(tb)
    small, large = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    if len(small) < 2:
        return False
    return sum(1 for t in small if any(_same_token(t, u) for u in large)) / len(small) >= 0.7


def flag_similar_follow_ups(items: list[dict]) -> None:
    """같은 묶음의 다른 활동 기록에 이미 있는 후속 조치와 비슷한 것을 뒤쪽 항목에 표시한다
    (`dup_follow_ups: [{"row", "task", "of"}]`). 지우지는 않는다: 사용자가 확인 화면에서 고른다."""
    seen: list[tuple[str, dict]] = []
    for item in items:
        if item.get("kind") != "activity":
            continue
        rows = (item.get("draft") or {}).get("follow_ups", [])
        flagged = []
        for i, row in enumerate(rows):
            for key, other in seen:
                if similar_tasks(row, other):
                    flagged.append({"row": i, "task": str(row.get("task", "")), "of": key})
                    break
        if flagged:
            item["dup_follow_ups"] = flagged
        seen.extend((item["key"], row) for row in rows if str(row.get("task", "")).strip())


# ---- 정리 시작·복구 ----

def _fail_analysis(db: Session, intake_id: int, job_id: int | None, message: str) -> bool:
    """정리 중인 묶음(그 작업이 지금 작업일 때만)을 실패로 바꾼다. 조건부 UPDATE 라 한 번만 반영된다."""
    same_job = Intake.job_id.is_(None) if job_id is None else Intake.job_id == job_id
    result = db.execute(
        update(Intake).where(Intake.id == intake_id, Intake.status == "analyzing", same_job)
        .values(status="failed", error=message)
    )
    db.commit()
    return result.rowcount == 1


def job_failure_message(job: AIJob | None) -> str:
    """실패한 정리 작업의 화면용 한국어 문구."""
    if job is None or job.status != "failed":
        return labels.ERROR_KINDS["unknown"]
    if job.error_kind == "bad_output" and job.error:
        return job.error
    return labels.ERROR_KINDS.get(job.error_kind or "", labels.ERROR_KINDS["unknown"])


def start_analysis(db: Session, intake: Intake, user: User) -> bool:
    """묶음을 정리 중으로 바꾸고 정리 작업을 넣는다. 올림·확인 대기·실패 상태에서만, 동시에 와도 한 번만 시작된다.
    읽을 수 있는 파일이 없으면 작업 없이 실패로 둔다 (False)."""
    db.refresh(intake)
    intake_id = intake.id
    startable = Intake.status.in_(("uploaded", "ready", "failed"))
    if not any(f.read_status == "ok" or (f.kind == "audio" and f.read_status == "pending_transcript") for f in intake.files):
        db.rollback()
        db.execute(update(Intake).where(Intake.id == intake_id, startable).values(status="failed", error=NO_READABLE))
        db.commit()
        db.refresh(intake)
        return False
    db.rollback()  # 읽기 트랜잭션을 끝내고 쓰기를 새로 시작한다
    job = AIJob(kind="intake_analyze", input={"intake_id": intake_id}, requester_id=user.id, priority=0, status="pending")
    db.add(job)
    db.flush()
    claimed = db.execute(
        update(Intake).where(Intake.id == intake_id, startable)
        .values(status="analyzing", job_id=job.id, plan=None, error="")
    )
    if claimed.rowcount != 1:  # 이미 정리 중이거나 끝난 묶음이다
        db.rollback()
        db.refresh(intake)
        return False
    # 새 정리를 시작하면 지난 받아쓰기의 퍼센트·실패 이유는 지운다 (묵은 값이 화면에 남지 않게)
    db.execute(update(IntakeFile).where(IntakeFile.intake_id == intake_id, IntakeFile.kind == "audio")
               .values(progress=None, read_error=None))
    db.commit()
    db.refresh(intake)
    return True


def finish_failed_analysis(db: Session, intake_id: int) -> bool:
    """작업이 끝났는데 묶음이 아직 정리 중이면 그 작업의 실패(또는 이상 종료)를 묶음에 기록한다."""
    intake = db.get(Intake, intake_id)
    if intake is None or intake.status != "analyzing":
        return False
    job = db.get(AIJob, intake.job_id) if intake.job_id is not None else None
    if job is not None and job.status not in ("failed", "done"):
        return False  # 아직 대기·진행 중이다 (작업이 없는 묶음은 멈춘 것이므로 아래에서 실패로 둔다)
    return _fail_analysis(db, intake_id, intake.job_id, job_failure_message(job))


def recover(db: Session) -> int:
    """앱을 시작할 때 정리 작업이 이미 실패했는데 정리 중으로 남은 묶음을 실패로 바꾼다."""
    ids = list(db.scalars(select(Intake.id).where(Intake.status == "analyzing")))
    return sum(finish_failed_analysis(db, i) for i in ids)


# ---- 정리안 확인과 저장 ----

SAVEABLE_KINDS = ("new_partner", "new_customer", "new_solution", "add_to_solution", "add_contact", "activity")
NOTHING_TO_SAVE = "저장할 항목이 없습니다."
APPLY_FAILED = "저장하지 못했습니다. 아무것도 저장되지 않았으니 잠시 후 다시 저장해 주세요. 계속 안 되면 관리자에게 알려 주세요."
APPLY_INTERRUPTED = "저장하는 도중 서버가 멈췄습니다. 아무것도 저장되지 않았으니 다시 저장해 주세요."
SAVE_SUPERSEDED = "다른 저장이나 다시 정리가 먼저 시작되어 이 저장은 취소했습니다. 화면을 새로 고쳐 확인해 주세요."
PLAN_CHANGED = "정리안이 그 사이 바뀌었습니다."
TARGET_GONE = "대상 솔루션이 삭제되어 자료를 추가하지 못했습니다."
CONTACT_TARGET_GONE = {"partner": "대상 파트너가 삭제되어 연락처를 추가하지 못했습니다.",
                       "customer": "대상 고객이 삭제되어 연락처를 추가하지 못했습니다."}
TARGET_CHANGED = "대상이 바뀌었거나 삭제되어 자료를 추가하지 못했습니다. 대상을 다시 고르세요."
CONTACT_TARGET_CHANGED = "대상이 바뀌었거나 삭제되어 연락처를 추가하지 못했습니다. 대상을 다시 고르세요."
CONTACT_EXISTS = "이미 있는 연락처"
NO_CONTACT_FIELD = "담당자·전화·이메일·주소 중 하나는 입력하세요."
_FILE_KIND_HINTS = (("intro", ("소개", "intro", "brochure", "카탈로그", "catalog")), ("spec", ("스펙", "spec", "사양", "규격", "datasheet")))


class NotReady(Exception):
    """확인 대기(ready) 상태가 아니거나 다른 요청이 먼저 저장을 시작했다."""


class Superseded(NotReady):
    """이 저장이 끝나기 전에 선점이 풀리거나 다른 저장·정리가 시작되어 이 저장을 취소했다."""


class ApplyFailed(Exception):
    """저장 도중 오류가 나 모두 되돌렸다. 메시지는 화면에 보일 한국어 문구."""


class _Fields:
    """제출된 폼. Starlette FormData 도, `{이름: 값 또는 값 목록}` dict 도 받는다 (파일 칸은 무시)."""

    def __init__(self, form):
        pairs = form.multi_items() if hasattr(form, "multi_items") else [
            (k, v) for k, vs in dict(form).items() for v in (vs if isinstance(vs, (list, tuple)) else [vs])
        ]
        self.data: dict[str, list[str]] = {}
        for name, value in pairs:
            if isinstance(value, str):
                self.data.setdefault(name, []).append(value)

    def flag(self, name: str) -> bool:
        return name in self.data

    def text(self, name: str, default: str = "") -> str:
        values = self.data.get(name)
        return values[0] if values else default

    def many(self, name: str) -> list[str]:
        return list(self.data.get(name, []))

    def has_prefix(self, prefix: str) -> bool:
        return any(name.startswith(prefix) for name in self.data)


def guess_file_kind(name: str) -> str:
    """파일 이름에서 솔루션 자료 종류를 짐작한다: 소개 / 스펙 / 관련."""
    lowered = name.lower()
    for kind, hints in _FILE_KIND_HINTS:
        if any(h in lowered for h in hints):
            return kind
    return "related"


def _digits(value) -> int | None:
    text = str(value if value is not None else "").strip()
    return int(text) if text.isascii() and text.isdecimal() and len(text) < 19 else None


def item_values(item: dict, fields: _Fields | None, file_names: dict[int, str]) -> dict:
    """항목 하나의 입력칸 값. `fields` 가 없으면 정리안의 기본값. 항목의 칸이 하나라도 제출됐으면 제출된 값을 쓰되
    빠진 글 칸은 기본값으로 채운다. 항목 종류·번호·파일 번호는 언제나 정리안(`item`)에서 온다."""
    key, kind = item["key"], item["kind"]
    use = fields is not None and fields.has_prefix(f"{key}__")

    def text(field: str, default) -> str:
        return fields.text(f"{key}__{field}", str(default or "")) if use else str(default or "")

    def many(field: str, default: list) -> list[str]:
        return fields.many(f"{key}__{field}") if use else [str(x) for x in default]

    v: dict = {"skip": bool(fields and fields.flag(f"skip_{key}"))}
    if kind in ("new_solution", "add_to_solution"):
        kinds = {}
        for n in item.get("files", []):
            guessed = guess_file_kind(file_names.get(n, ""))
            picked = text(f"kind_{n}", guessed)
            kinds[n] = picked if picked in labels.FILE_KINDS else guessed
        v["kinds"] = kinds
    if kind == "new_partner":
        v.update(name=text("name", item.get("name")), contact=text("contact", item.get("contact")),
                 description=text("description", item.get("description")), memo=text("memo", item.get("memo")))
    elif kind == "new_customer":
        v.update(name=text("name", item.get("name")), industry=text("industry", item.get("industry")),
                 contact=text("contact", item.get("contact")), memo=text("memo", item.get("memo")))
    elif kind == "new_solution":
        v.update(name=text("name", item.get("name")), summary=text("summary", item.get("summary")),
                 description=text("description", item.get("description")), tags=text("tags", ", ".join(item.get("tags") or [])),
                 partner=text("partner", item.get("partner_id") or ""), partner_new=text("partner_new", item.get("partner_new")))
    elif kind == "add_to_solution":
        v["solution"] = text("solution", item.get("solution_id"))
    elif kind == "add_contact":
        v["target"] = text("target", item.get("target_id"))
        for field in CONTACT_FIELDS:
            v[field] = _flat(text(field, item.get(field)))[:CONTACT_CAPS[field]]
        v["target_ok"] = bool(fields and fields.flag(f"{key}__target_ok"))
    elif kind == "activity":
        d = item.get("draft") or {}
        fus = [f for f in d.get("follow_ups", []) if str(f.get("task", "")).strip()]
        dss = [x for x in d.get("demand_signals", []) if str(x.get("content", "")).strip()]
        matched = item.get("matched", {})
        v.update(
            kind=text("kind", d.get("kind")), date=text("date", d.get("date")), title=text("title", d.get("title")),
            summary_md=text("summary_md", item.get("summary_md")), raw_text=text("raw_text", item.get("raw_text")),
            fu_task=many("fu_task", [f.get("task", "") for f in fus]), fu_owner=many("fu_owner", [f.get("owner", "") for f in fus]),
            fu_due=many("fu_due", [f.get("due", "") for f in fus]),
            ds_customer=many("ds_customer", [x.get("customer", "") for x in dss]),
            ds_content=many("ds_content", [x.get("content", "") for x in dss]),
            solution_ids=many("solution_ids", matched.get("solutions", [])),
            customer_ids=many("customer_ids", matched.get("customers", [])),
            business_ids=many("business_ids", matched.get("businesses", [])),
            partner_ids=many("partner_ids", matched.get("partners", [])),
        )
        names = {rel: [n.strip() for n in many(f"new_{rel}", []) if n.strip()] for rel in ("solutions", "customers", "partners")}
        v["new_options"] = {}
        for rel, checked in names.items():
            candidates = list(item.get("new", {}).get(rel, []))
            v["new_options"][rel] = [(n, n in checked) for n in candidates] + [(n, True) for n in checked if n not in candidates]
        v["new_names"] = {f"new_{rel}": [n for n, on in opts if on] for rel, opts in v["new_options"].items()}
        v["fu_rows"] = [{"task": t, "owner": o, "due": d_} for t, o, d_ in _rows(v["fu_task"], v["fu_owner"], v["fu_due"])]
        flagged = {d["row"] for d in item.get("dup_follow_ups", []) if isinstance(d.get("row"), int)}
        v["fu_drop"] = {n for n in (_digits(x) for x in (fields.many(f"{key}__fu_drop") if use else [])) if n in flagged}
        v["ds_rows"] = [{"customer": c, "content": t} for c, t in _rows(v["ds_customer"], v["ds_content"])]
        v["selected"] = {t: [n for n in (_digits(i) for i in v[f"{t}_ids"]) if n is not None]
                         for t in ("solution", "customer", "business", "partner")}
    return v


def kept_follow_up_positions(item: dict, v: dict) -> list[int]:
    """폼의 후속 조치 줄 번호(위치) 중 저장할 것. `v["fu_drop"]` 은 정리안에서 표시된 행 번호(빈 할 일을 뺀 줄 순서가
    아니라 초안의 행 번호)이므로, 표시된 행이 폼에서 몇 번째 줄인지 정리안의 초안으로 센다."""
    rows = (item.get("draft") or {}).get("follow_ups", [])
    tasks = v.get("fu_task", [])
    drop_positions = set()
    for n in v.get("fu_drop", ()):
        if n >= len(rows) or not str(rows[n].get("task", "")).strip():
            continue
        pos = sum(1 for r in rows[:n] if str(r.get("task", "")).strip())
        # 그 줄의 글을 사용자가 고쳐 다른 할 일이 됐다면 빼지 않는다
        if pos < len(tasks) and " ".join(str(tasks[pos]).split()) == " ".join(str(rows[n]["task"]).split()):
            drop_positions.add(pos)
    return [i for i in range(len(v["fu_task"])) if i not in drop_positions]


def _rows(*columns: list[str]) -> list[tuple[str, ...]]:
    count = max((len(c) for c in columns), default=0)
    return [tuple((c[i] if i < len(c) else "").strip() for c in columns) for i in range(count)]


def item_error(item: dict, v: dict) -> str | None:
    """저장할 수 없는 입력이면 화면에 보일 문구."""
    label = labels.INTAKE_ITEM_KINDS.get(item["kind"], item["kind"])
    if item["kind"] in ("new_partner", "new_customer", "new_solution") and not v["name"].strip():
        return f"{label}: 이름을 입력하세요."
    if item["kind"] == "add_contact" and not any(v[f].strip() for f in USABLE_CONTACT_FIELDS):
        return f"{label}: {NO_CONTACT_FIELD}"
    if item["kind"] == "activity" and not v["raw_text"].strip() and not v["summary_md"].strip():
        return f"{label}: 메모(원문)나 정리본을 입력하세요."
    return None


def _file_names(intake: Intake) -> dict[int, str]:
    return {file_number(intake, f): f.original_name for f in intake.files}


_PLAIN_MD = [(re.compile(r"!?\[([^\]]*)\]\([^)]*\)"), r"\1"), (re.compile(r"(\*\*|__|`|~~)"), ""),
             (re.compile(r"^\s*(#{1,6}|[-*+>]|\d+[.)])\s+"), "")]


def plain_lines(md: str, count: int = 3) -> list[str]:
    """마크다운 정리본의 앞 몇 줄을 기호 없는 글자로 (요약용). 제목 줄과 참석자 목록은 건너뛴다."""
    lines: list[str] = []
    skipping = False
    for raw in str(md or "").splitlines():
        if re.match(r"\s*#{1,6}\s", raw):
            skipping = raw.lstrip("# \t").startswith("참석자")
            continue
        if skipping:
            continue
        line = raw
        for pattern, repl in _PLAIN_MD:
            line = pattern.sub(repl, line)
        line = line.strip()
        if line:
            lines.append(line)
        if len(lines) >= count:
            break
    return lines


_SIG_SKIP = {"skip", "fu_drop", "fu_task", "fu_owner", "fu_due", "ds_customer", "ds_content", "selected", "new_names"}


def _signature(v: dict) -> dict:
    """사용자가 고쳤는지 비교할 값 (빈 줄·줄바꿈 방식·선택 순서 차이는 무시)."""
    def norm(x):
        return x.replace("\r\n", "\n") if isinstance(x, str) else x

    sig = {}
    for k, val in v.items():
        if k in _SIG_SKIP:
            continue
        if k.endswith("_ids"):
            sig[k] = sorted(str(x) for x in val)
        elif k in ("fu_rows", "ds_rows"):
            sig[k] = [tuple(norm(x) for x in r.values()) for r in val if any(x for x in r.values())]
        else:
            sig[k] = norm(val)
    return sig


CONTACT_MODELS = {"partner": Partner, "customer": Customer}


def contact_body(v: dict) -> str:
    """덧붙일 연락처 줄의 본문: `담당자 직책 · 전화 · 이메일 · 주소 · 메모` (빈 칸은 건너뛰고, 줄바꿈은 공백으로)."""
    who = " ".join(x for x in (_flat(v.get("person", "")), _flat(v.get("role", ""))) if x)
    parts = [who] + [_flat(v.get(f, "")) for f in ("phone", "email", "address", "note")]
    return " · ".join(p for p in parts if p)


def contact_source(files: list[IntakeFile]) -> str:
    """연락처 줄 끝에 적을 출처 말: 이름에 "명함" 이 든 파일이 있으면 명함, 아니면 파일 종류(사진·문서·음성)."""
    if any("명함" in f.original_name for f in files):
        return "명함"
    kinds = list(dict.fromkeys(labels.INTAKE_FILE_KINDS.get(f.kind, "") for f in files))
    return "·".join(k for k in kinds if k)


def contact_line(v: dict, files: list[IntakeFile], today: date) -> str:
    """대상의 연락처 칸에 덧붙일 한 줄. 본문이 비면 빈 글."""
    body = contact_body(v)
    if not body:
        return ""
    source = contact_source(files)
    return f"{body} ({source + ', ' if source else ''}{today.isoformat()} 올리기)"


def _item_files(intake: Intake, item: dict) -> list[IntakeFile]:
    by_number = {file_number(intake, f): f for f in intake.files}
    return [by_number[n] for n in item.get("files", []) if n in by_number]


def _brief(db: Session, item: dict, v: dict, names: dict[int, str], intake: Intake) -> dict:
    """접힌 카드에 보일 요약."""
    from app import records

    kind = item["kind"]
    b: dict = {"title": "", "facts": []}
    if kind == "activity":
        b["title"] = v["title"].strip() or "(제목 없음)"
        b["date"], b["kind_label"] = v["date"], labels.ACTIVITY_KINDS.get(v["kind"], v["kind"])
        b["lines"] = plain_lines(v["summary_md"])
        b["fu_count"] = sum(1 for r in v["fu_rows"] if r["task"])
        b["ds_count"] = sum(1 for r in v["ds_rows"] if r["content"])
        linked = []
        for t, (model, _) in records.LINK_TYPES.items():
            for number in v["selected"][t]:
                obj = db.get(model, number)
                if obj is not None:
                    linked.append(obj.name)
        b["linked"] = linked
        b["new_names"] = [n for rel in ("solutions", "customers", "partners") for n, on in v["new_options"][rel] if on]
        b["new_candidates"] = [n for rel in ("solutions", "customers", "partners") for n, on in v["new_options"][rel] if not on]
        dups = {str(d.get("task", "")).strip() for d in item.get("dup_follow_ups", [])}
        titles = {i.get("key"): str((i.get("draft") or {}).get("title") or "").strip() or "(제목 없음)"
                  for i in (intake.plan or {}).get("items", []) if i.get("kind") == "activity"}
        b["dup_of"] = {str(d.get("task", "")).strip(): titles.get(d.get("of"), "") for d in item.get("dup_follow_ups", [])}
        b["dup_tasks"] = dups
        now = {r["task"].strip() for r in v["fu_rows"]}
        b["dup_rows"] = [{"row": d["row"], "task": str(d.get("task", "")).strip(), "checked": d["row"] in v["fu_drop"]}
                         for d in item.get("dup_follow_ups", []) if str(d.get("task", "")).strip() in now]
        b["dup_count"] = sum(1 for r in v["fu_rows"] if r["task"].strip() in dups)
    elif kind == "new_solution":
        b["title"] = v["name"].strip() or "(이름 없음)"
        partner = db.get(Partner, int(v["partner"])) if str(v["partner"]).isdecimal() and len(str(v["partner"])) < 18 else None
        b["partner"] = partner.name if partner is not None else v["partner_new"].strip()
        b["summary"] = v["summary"].strip()
        b["tags"] = [t.strip() for t in v["tags"].split(",") if t.strip()]
    elif kind == "add_to_solution":
        number = _digits(v["solution"])
        target = db.get(Solution, number) if number is not None else None
        b["title"] = target.name if target is not None else "(대상 솔루션을 고르세요)"
    elif kind == "add_contact":
        number = _digits(v["target"])
        target = db.get(CONTACT_MODELS[item["target_type"]], number) if number is not None else None
        stored = item.get("target_name")
        # 정리안을 만든 뒤 대상의 이름이 바뀌었으면 (또는 번호를 다른 기록이 물려받았으면) 같은 대상이라고 보여 주지 않는다
        same_id = not str(v["target"]).strip() or number == item.get("target_id")
        current = db.get(CONTACT_MODELS[item["target_type"]], item.get("target_id")) if same_id and item.get("target_id") is not None else None
        b["target_changed"] = bool(current is not None and stored and normalize_name(stored) != normalize_name(current.name))
        b["target_current"] = current.name if b["target_changed"] else ""
        b["title"] = "(대상을 다시 고르세요)" if b["target_changed"] else target.name if target is not None else "(대상을 고르세요)"
        b["line"] = contact_line(v, _item_files(intake, item), date.today())
        b["type_label"] = "파트너" if item["target_type"] == "partner" else "고객"
    elif kind in ("new_customer", "new_partner"):
        b["title"] = v["name"].strip() or "(이름 없음)"
        b["contact"] = v["contact"].strip()
        b["industry"] = v.get("industry", "").strip()
    return b


def review_cards(db: Session, intake: Intake, form=None) -> list[dict]:
    """정리안 확인 화면의 항목 카드: `{"item", "v", "files", "ambiguous", "brief", "open"}`. `form` 이 있으면 제출한 값을 유지한다.
    `open` 은 고치기를 펼쳐 둘 카드 (저장할 수 없는 입력이거나, 제출한 값이 기본값과 다른 카드)."""
    from app import records

    fields = _Fields(form) if form is not None else None
    names = _file_names(intake)
    cards = []
    for item in (intake.plan or {}).get("items", []):
        v = item_values(item, fields, names)
        card = {"item": item, "v": v, "files": [(n, names.get(n, "")) for n in item.get("files", [])], "ambiguous": {},
                "file_ids": {file_number(intake, f): f.id for f in intake.files},
                "file_status": {file_number(intake, f): f.read_status for f in intake.files}}
        if item["kind"] == "activity":
            for t, (model, rel) in records.LINK_TYPES.items():
                card["ambiguous"][t] = records.ambiguous_view(db, model, item.get("ambiguous", {}).get(rel, []))
        if item["kind"] != "unplaced":
            card["brief"] = _brief(db, item, v, names, intake)
            card["open"] = item_error(item, v) is not None or card["brief"].get("target_changed", False) or (
                fields is not None and _signature(v) != _signature(item_values(item, None, names)))
        cards.append(card)
    return cards


def _release_once(db: Session, intake_id: int, token: str, message: str) -> None:
    db.execute(
        update(Intake).where(Intake.id == intake_id, Intake.status == "applying", Intake.apply_token == token)
        .values(status="ready", error=message, apply_token=None)
    )
    db.commit()


def _release(db: Session, intake_id: int, token: str, message: str) -> None:
    """이 저장(token)이 선점한 묶음을 다시 확인 대기로 돌린다 (오류 문구와 함께). 그 사이 다른 저장이 선점했거나 다시 정리가 시작됐으면
    건드리지 않는다. 실패하면 한 번 더 해 보고, 그래도 안 되면 기록만 남긴다 (그 묶음은 화면의 "다시 확인" 이나 서버를 다시 시작할 때 풀린다)."""
    for attempt in (1, 2):
        try:
            _release_once(db, intake_id, token, message)
            return
        except Exception:  # noqa: BLE001
            db.rollback()
            log.exception("올리기 묶음 %s 를 확인 대기로 되돌리지 못했습니다 (시도 %s)", intake_id, attempt)


APPLYING_STALE = timedelta(minutes=5)


def is_stale_applying(intake: Intake) -> bool:
    return intake.status == "applying" and datetime.now() - intake.updated_at > APPLYING_STALE


def recheck_applying(db: Session, intake: Intake) -> bool:
    """오래 저장 중으로 남은 묶음을 확인 대기로 되돌린다. 조건부 UPDATE 라 그 사이 끝났거나 아직 새로운 저장은 건드리지 않는다."""
    result = db.execute(
        update(Intake).where(Intake.id == intake.id, Intake.status == "applying", Intake.updated_at < datetime.now() - APPLYING_STALE)
        .values(status="ready", error=APPLY_INTERRUPTED, apply_token=None)
    )
    db.commit()
    db.refresh(intake)
    return result.rowcount == 1


def recover_applying(db: Session) -> int:
    """앱을 시작할 때 저장 중으로 남은 묶음을 확인 대기로 되돌린다. 저장은 한 번에 커밋되므로 그 묶음의 기록은 만들어지지 않았다."""
    result = db.execute(update(Intake).where(Intake.status == "applying").values(status="ready", error=APPLY_INTERRUPTED, apply_token=None))
    db.commit()
    return result.rowcount


def discard(db: Session, settings: Settings, intake: Intake) -> bool:
    """묶음을 버린다 (작업 폴더만 지우고 원본은 둔다). 저장 중이거나 저장이 끝난 묶음이면 False. 이미 버렸으면 True."""
    intake_id = intake.id
    result = db.execute(
        update(Intake).where(Intake.id == intake_id, Intake.status.in_(("uploaded", "analyzing", "ready", "failed")))
        .values(status="discarded")
    )
    db.commit()
    db.refresh(intake)
    if result.rowcount == 1:
        remove_workdir(settings, intake)
        return True
    return intake.status == "discarded"


class _Run:
    """저장 한 번의 진행 기록: 결과 목록, 이름으로 다시 찾을 기록, 지식 사본을 갱신할 기록."""

    def __init__(self, db: Session, settings: Settings, intake: Intake, user: User, extracted: dict[int, ExtractResult], copied: list[Path]):
        self.db, self.settings, self.intake, self.user = db, settings, intake, user
        self.extracted, self.copied = extracted, copied
        self.by_number = {file_number(intake, f): f for f in intake.files}
        self.result: list[dict] = []
        self.skipped: list[dict] = []
        self.unsaved: list[dict] = []  # 저장하지 않은 파일 (정리하지 못한 파일, 제외한 항목의 파일)
        self.known: dict[str, dict[str, object]] = {"solutions": {}, "customers": {}, "partners": {}}
        self.sync: list = []
        self.file_texts: list[tuple] = []
        self.attached: set[tuple[int, int]] = set()
        # 이 저장 전에 있던 기록의 번호. 그 사이 지워진 번호를 이 저장에서 새로 만든 기록이 물려받아도 (SQLite 는 번호를 다시 쓴다) 섞이지 않는다.
        self.existing = {m: set(db.scalars(select(m.id))) for m in (Solution, Customer, Partner, Business)}

    def existing_id(self, model, value) -> int | None:
        number = _digits(value)
        return number if number in self.existing[model] else None

    def remember(self, rel: str, obj) -> None:
        self.known[rel][normalize_name(obj.name)] = obj
        if obj not in self.sync:
            self.sync.append(obj)

    def attach(self, solution: Solution, item: dict, v: dict) -> int:
        """항목의 파일을 솔루션 자료로 복사한다 (원본은 그대로 둔다). 만든 자료 수."""
        from app import records

        made = 0
        for number in item.get("files", []):
            f = self.by_number.get(number)
            if f is None or f.read_status != "ok" or (solution.id, f.id) in self.attached:
                continue
            src = stored_path(self.settings, f)

            def write(path: Path, src: Path = src) -> int:
                path.parent.mkdir(parents=True, exist_ok=True)
                self.copied.append(path)  # 복사하다 실패해도 지울 수 있게 먼저 적어 둔다
                shutil.copyfile(src, path)
                return path.stat().st_size

            saved = records.new_solution_file(self.db, self.settings, solution, f.original_name, v["kinds"][number], self.user,
                                              write, extracted=self.extracted.get(f.id))
            sf, extract = saved  # 크기 제한 없이 복사하므로 None 이 아니다
            self.attached.add((solution.id, f.id))
            self.file_texts.append((sf, extract))
            if not f.target_type:
                f.target_type, f.target_id = "solution", solution.id
            made += 1
        return made

    def partner_for(self, v: dict):
        """새 솔루션의 파트너: 고른 등록 파트너, 없으면 새 이름 (같은 이름이 있으면 그것)."""
        from app import records

        chosen = self.existing_id(Partner, v["partner"])
        if chosen is not None:
            return self.db.get(Partner, chosen)
        if v["partner_new"].strip():
            partner, created = records.get_or_create(self.db, Partner, v["partner_new"], self.user)
            if created:
                self.result.append({"type": "partner", "id": partner.id, "label": partner.name})
            if partner is not None:
                self.remember("partners", partner)
            return partner
        return None


def _item_title(item: dict, v: dict) -> str:
    return str(v.get("title") or v.get("name") or item.get("solution_name") or item.get("target_name") or "").strip()


def _unsaved_files(intake: Intake, chosen: list[tuple[dict, dict]], fields: _Fields, names: dict[int, str]) -> list[dict]:
    """저장하지 않은 파일: 정리하지 못한 파일과, 제외한 항목에만 쓰인 파일. 확인 화면이 작업 폴더 없이 보여 줄 수 있게
    저장 결과에 이름·이유·파일 번호를 적어 둔다."""
    by_number = {file_number(intake, f): f for f in intake.files}
    used = {n for item, _ in chosen for n in item.get("files", [])}
    out: list[dict] = []
    seen: set[int] = set()
    for item in (intake.plan or {}).get("items", []):
        if item["kind"] == "unplaced":
            reason = str(item.get("reason") or "").strip() or NOT_PLACED
        elif item["kind"] in SAVEABLE_KINDS and fields.flag(f"skip_{item['key']}"):
            title = _item_title(item, item_values(item, fields, names))
            reason = f"직접 제외한 항목 ({labels.INTAKE_ITEM_KINDS[item['kind']]}{': ' + title if title else ''})"
        else:
            continue
        for n in item.get("files", []):
            f = by_number.get(n)
            if f is None or n in used or n in seen:
                continue
            seen.add(n)
            out.append({"type": "unsaved", "id": None, "label": f.original_name, "reason": reason, "file_id": f.id})
    return out


def _extract_for_solutions(settings: Settings, intake: Intake, chosen: list[tuple[dict, dict]]) -> dict[int, ExtractResult]:
    """솔루션 자료가 될 파일의 글을 미리 읽는다 (쓰기 트랜잭션을 길게 잡지 않으려고 저장 전에). 사진 등은 읽지 않고 보관만."""
    by_number = {file_number(intake, f): f for f in intake.files}
    out: dict[int, ExtractResult] = {}
    for item, _ in chosen:
        if item["kind"] not in ("new_solution", "add_to_solution"):
            continue
        for number in item.get("files", []):
            f = by_number.get(number)
            if f is None or f.read_status != "ok" or f.id in out:
                continue
            out[f.id] = extract_text(stored_path(settings, f)) if f.kind == "document" else ExtractResult("unsupported", "")
    return out


def _phone_key(text: str) -> str:
    """전화번호 비교용 숫자: 숫자만 남기고, 앞의 국가 번호 82 는 0 으로 (+82 10-1234-5678 = 010-1234-5678)."""
    digits = _digits_only(text)
    if digits.startswith("82") and len(digits) > 9:
        digits = "0" + digits[2:].lstrip("0")
    return digits


def _phone_keys(text: str) -> set[str]:
    """글 안의 전화번호들 (숫자 비교용). 전화 모양이 아니어도 5자리 이상 숫자 덩어리는 번호로 본다 (내선)."""
    found = {_phone_key(m.group(0)) for m in _PHONE.finditer(text)}
    found |= {m.group(0) for m in re.finditer(r"(?<![\d\-])\d{5,}(?![\d\-])", text)}
    return {k for k in found if len(k) >= 5}


def _email_keys(text: str) -> set[str]:
    return {m.group(0).lower() for m in _EMAIL.finditer(text)}


def _already_listed(existing: str, v: dict) -> bool:
    """덧붙일 연락처가 이미 적혀 있는가. 새 연락처의 전화·이메일이 하나라도 있고 모두 이미 있으며(전화는 숫자만·국가 번호 맞춰,
    이메일은 대소문자 무시·주소 전체 비교), 담당자 이름이 있으면 그 이름도 이미 적혀 있을 때만 그렇다.
    전화·이메일이 하나도 없으면 같은 본문이 이미 있는지만 본다."""
    phones = _phone_keys(v["phone"]) or ({_phone_key(v["phone"])} if len(_phone_key(v["phone"])) >= 5 else set())
    emails = _email_keys(v["email"]) or ({v["email"].strip().lower()} if v["email"].strip() else set())
    if not phones and not emails:
        body = contact_body(v)
        return bool(body) and body.lower() in _flat(existing).lower()
    if not phones <= _phone_keys(existing) or not emails <= _email_keys(existing):
        return False
    person = re.sub(r"\s+", "", _flat(v.get("person", ""))).lower()
    return not person or person in re.sub(r"\s+", "", _flat(existing)).lower()


def _pick_target(run: _Run, model, raw, plan_id) -> tuple[int | None, bool]:
    """대상 고르기: (번호, 정리안의 대상인가). 폼에 비지 않은 값이 있고 정리안의 번호와 다르면 직접 고른 것이라
    같은 종류의 실제 기록일 때만 쓴다 (아니면 None: 정리안 대상으로 돌아가지 않고 건너뛴다). 폼에 값이 없거나 비어 있으면 정리안의 대상."""
    text = str(raw if raw is not None else "").strip()
    if text and _digits(text) != _digits(plan_id):
        return run.existing_id(model, text), False
    return run.existing_id(model, plan_id), True


def _skip(run: _Run, kind: str, reason: str, files: list[dict], label: str = "") -> None:
    title = f"{labels.INTAKE_ITEM_KINDS[kind]}: {label + ' — ' if label else ''}{reason}"
    run.skipped.append({"type": "skipped", "id": None, "label": title, "reason": reason, "files": files})


def _append_contact(run: _Run, item: dict, v: dict) -> None:
    """연락처 덧붙이기: 대상의 연락처 칸 끝에 한 줄을 더한다. 기존 글은 바꾸지 않고, 같은 전화·이메일이 있으면 건너뛴다.
    읽기-수정-쓰기 사이에 다른 저장이 끼어도 한 줄도 잃지 않게, 쓰기 잠금을 먼저 잡고 다시 읽은 뒤 SQL 한 문장으로 덧붙인다."""
    model, db = CONTACT_MODELS[item["target_type"]], run.db
    table = model.__table__
    files = [{"id": f.id, "name": f.original_name} for f in _item_files(run.intake, item)]
    gone = CONTACT_TARGET_GONE[item["target_type"]]
    target, from_plan = _pick_target(run, model, v["target"], item.get("target_id"))
    if target is None:
        _skip(run, "add_contact", gone, files)
        return
    # 쓰기 잠금: 이 문장 뒤의 읽기는 다른 저장이 이미 끝낸 값을 본다. 행이 없으면 그 사이 지워진 것이다
    if db.execute(update(table).where(table.c.id == target).values(id=table.c.id)).rowcount != 1:
        _skip(run, "add_contact", gone, files)
        return
    obj = db.get(model, target)
    if obj is None:
        _skip(run, "add_contact", gone, files)
        return
    db.refresh(obj)
    stored = item.get("target_name")
    # 번호를 다시 쓰는 다른 기록이거나 이름이 바뀐 기록을 정리안의 번호로 가리키게 된 경우 (사용자가 같은 대상이라고 확인했으면 쓴다)
    if from_plan and not v.get("target_ok") and stored and normalize_name(stored) != normalize_name(obj.name):
        _skip(run, "add_contact", CONTACT_TARGET_CHANGED, files)
        return
    if _already_listed(obj.contact or "", v):
        _skip(run, "add_contact", CONTACT_EXISTS, files, obj.name)
        return
    line = contact_line(v, _item_files(run.intake, item), date.today())
    col = table.c.contact
    appended = case((func.trim(func.coalesce(col, ""), " \t\r\n") == "", line), else_=func.rtrim(col, "\r\n") + "\n" + line)
    db.execute(update(table).where(table.c.id == target).values(contact=appended))
    db.refresh(obj)  # 덧붙인 값을 읽어 와야 지식 사본이 새 값으로 써진다
    for f in _item_files(run.intake, item):
        if not f.target_type:
            f.target_type, f.target_id = item["target_type"], obj.id
    run.remember("partners" if item["target_type"] == "partner" else "customers", obj)
    run.result.append({"type": item["target_type"], "id": obj.id, "label": f"{obj.name} (연락처 추가)"})


def _create_records(run: _Run, chosen: list[tuple[dict, dict]]) -> None:
    """고른 항목으로 기록을 만든다 (커밋하지 않는다): 파트너, 고객, 솔루션, 솔루션 자료, 활동 기록 순서."""
    from app import records

    db, user = run.db, run.user
    partners_for: dict[str, object] = {}
    for item, v in chosen:
        if item["kind"] == "new_partner":
            partner, created = records.get_or_create(db, Partner, v["name"], user, contact=v["contact"].strip(),
                                                     description=v["description"].strip(), memo=v["memo"].strip())
            if created:
                run.result.append({"type": "partner", "id": partner.id, "label": partner.name})
            run.remember("partners", partner)
    for item, v in chosen:
        if item["kind"] == "new_solution":
            partners_for[item["key"]] = run.partner_for(v)
    for item, v in chosen:
        if item["kind"] == "new_customer":
            customer, created = records.get_or_create(db, Customer, v["name"], user, industry=v["industry"].strip(),
                                                      contact=v["contact"].strip(), memo=v["memo"].strip())
            if created:
                run.result.append({"type": "customer", "id": customer.id, "label": customer.name})
            run.remember("customers", customer)

    for item, v in chosen:
        if item["kind"] != "new_solution":
            continue
        partner = partners_for.get(item["key"])
        tags = ",".join(t for t in (p.strip() for p in v["tags"].split(",")) if t)
        solution, created = records.get_or_create(
            db, Solution, v["name"], user, summary=v["summary"].strip(), description=v["description"].strip(), tags=tags,
            partner_id=partner.id if partner is not None else None,
        )
        made = run.attach(solution, item, v)
        run.remember("solutions", solution)
        label = solution.name if created else f"{solution.name} (이미 등록된 솔루션에 자료 {made}개 추가)"
        run.result.append({"type": "solution", "id": solution.id, "label": label})
    for item, v in chosen:
        if item["kind"] != "add_to_solution":
            continue
        target, from_plan = _pick_target(run, Solution, v["solution"], item.get("solution_id"))
        reason = TARGET_GONE
        if target is not None and from_plan:
            current = db.get(Solution, target)
            stored = item.get("solution_name")
            # 번호를 다시 쓰는 다른 솔루션이거나 이름이 바뀐 경우는 대상이 바뀐 것으로 본다
            if current is None or (stored and normalize_name(stored) != normalize_name(current.name)):
                target, reason = None, TARGET_CHANGED
        if target is None:
            files = [{"id": run.by_number[n].id, "name": run.by_number[n].original_name} for n in item.get("files", []) if n in run.by_number]
            _skip(run, "add_to_solution", reason, files)
            continue
        solution = db.get(Solution, target)
        made = run.attach(solution, item, v)
        run.remember("solutions", solution)
        run.result.append({"type": "solution", "id": solution.id, "label": f"{solution.name} (자료 {made}개 추가)"})
    for item, v in chosen:
        if item["kind"] == "add_contact":
            _append_contact(run, item, v)

    db.flush()
    for item, v in chosen:
        if item["kind"] != "activity":
            continue
        keep = kept_follow_up_positions(item, v)

        def pick(column: list[str], keep=keep) -> list[str]:
            return [column[i] if i < len(column) else "" for i in keep]
        a = Activity(created_by_id=user.id, raw_text=v["raw_text"].strip())
        db.add(a)
        ids = {t: [i for i in v[f"{t}_ids"] if run.existing_id(model, i) is not None] for t, (model, _) in records.LINK_TYPES.items()}
        for rel, t in (("solutions", "solution"), ("customers", "customer"), ("partners", "partner")):
            for name in item.get("new", {}).get(rel, []):  # 이 묶음에서 만들었거나 같은 이름으로 찾은 기록과도 연결한다
                obj = run.known[rel].get(normalize_name(name))
                if obj is not None and str(obj.id) not in ids[t]:
                    ids[t].append(str(obj.id))
        records.apply_activity_fields(
            db, user, a, kind=v["kind"], date_=v["date"], title=v["title"], raw_text=v["raw_text"].strip(), summary_md=v["summary_md"],
            fu_task=pick(v["fu_task"]), fu_owner=pick(v["fu_owner"]), fu_due=pick(v["fu_due"]), fu_done=[],
            ds_customer=v["ds_customer"], ds_content=v["ds_content"],
            solution_ids=ids["solution"], customer_ids=ids["customer"], business_ids=ids["business"], partner_ids=ids["partner"],
            new_solutions=v["new_names"]["new_solutions"], new_customers=v["new_names"]["new_customers"],
            new_partners=v["new_names"]["new_partners"],
        )
        db.flush()
        for number in item.get("files", []):
            f = run.by_number.get(number)
            if f is not None:  # 활동 기록이 우선: 솔루션에는 자기 사본이 이미 있다
                f.target_type, f.target_id = "activity", a.id
        run.result.append({"type": "activity", "id": a.id, "label": a.title or "(제목 없음)"})
        run.sync.append(a)
    run.result.extend(run.skipped)
    run.result.extend(run.unsaved)


def _sync_knowledge(db: Session, mirror: KnowledgeMirror, run: _Run) -> None:
    """커밋 뒤에 만들거나 바뀐 기록과, 그 사본에 이름이 적히는 연결된 기록의 지식 사본을 다시 쓴다."""
    from app import records

    objs: list = []
    seen: set = set()

    def add(obj) -> None:
        if obj is not None and (type(obj), obj.id) not in seen:
            seen.add((type(obj), obj.id))
            objs.append(obj)

    for obj in run.sync:
        add(obj)
        if isinstance(obj, Activity):
            for related in records.linked(obj):
                add(related)
        if isinstance(obj, Solution):
            add(obj.partner)
    for obj in objs:
        db.refresh(obj)
    for sf, extract in run.file_texts:
        if extract.status == "ok":
            mirror.write_file_text(sf, file_markdown(sf, extract.text))
    for obj in objs:
        mirror.sync(obj)


def apply(db: Session, settings: Settings, mirror: KnowledgeMirror, intake: Intake, user: User, form) -> list[dict]:
    """확인 화면의 폼 값을 반영해 기록을 만든다. `[{"type", "id", "label"}]` (저장하지 못한 항목은 type "skipped").

    항목 종류·번호·파일 번호·솔루션 번호는 저장된 정리안(`intake.plan`)에서만 오고, 폼은 고친 글 값, 제외 표시,
    연결·대상 선택만 준다 (고른 번호는 실제로 있는 기록일 때만 쓴다). 입력 오류와 "저장할 항목이 없습니다." 는 ValueError,
    확인 대기 상태가 아니거나 다른 요청이 먼저 저장하면 NotReady, 저장 도중 오류는 ApplyFailed (아무것도 남기지 않고 ready 로 돌린다)."""
    if intake.status != "ready":
        raise NotReady
    plan = intake.plan
    fields, names = _Fields(form), _file_names(intake)
    chosen: list[tuple[dict, dict]] = []
    for item in (plan or {}).get("items", []):
        if item["kind"] not in SAVEABLE_KINDS or fields.flag(f"skip_{item['key']}"):
            continue
        v = item_values(item, fields, names)
        if error := item_error(item, v):
            raise ValueError(error)
        chosen.append((item, v))
    if not chosen:
        raise ValueError(NOTHING_TO_SAVE)

    intake_id = intake.id
    # 느린 글 추출은 선점 전에 한다 (선점 구간을 짧게 두어 "다시 확인" 과 겹칠 일을 줄인다). 새 기록이 필요 없는 일이다.
    try:
        extracted = _extract_for_solutions(settings, intake, chosen)
    except Exception as e:  # noqa: BLE001 - 아직 선점 전이라 되돌릴 것이 없다
        db.rollback()
        log.exception("올리기 묶음 %s 글 추출 실패", intake_id)
        raise ApplyFailed(APPLY_FAILED) from e
    db.commit()  # 읽기 트랜잭션을 끝내고, 선점 UPDATE 를 짧은 쓰기 트랜잭션으로 한다
    token = secrets.token_hex(16)
    claimed = db.execute(
        update(Intake).where(Intake.id == intake_id, Intake.status == "ready")
        .values(status="applying", error="", apply_token=token)
    )
    db.commit()
    if claimed.rowcount != 1:
        db.refresh(intake)
        raise NotReady

    copied: list[Path] = []
    try:
        db.refresh(intake)
        if intake.plan != plan:  # 선점 사이에 다시 정리되어 정리안이 바뀌었다
            raise NotReady
        run = _Run(db, settings, intake, user, extracted, copied)
        run.unsaved = _unsaved_files(intake, chosen, fields, names)
        _create_records(run, chosen)
        # 끝내는 전환은 내 선점일 때만 된다: 그 사이 "다시 확인" 으로 풀렸거나 다른 저장·정리가 시작됐으면 모두 되돌린다
        done = db.execute(
            update(Intake).where(Intake.id == intake_id, Intake.status == "applying", Intake.apply_token == token)
            .values(status="applied", result=list(run.result), applied_at=datetime.now(), error="", apply_token=None)
        )
        if done.rowcount != 1:
            raise Superseded(SAVE_SUPERSEDED)
        db.commit()
    except BaseException as e:
        for path in copied:  # 파일을 먼저 지운다 (rollback 이 실패해도 복사본이 남지 않게)
            path.unlink(missing_ok=True)
            try:
                path.parent.rmdir()  # 새 솔루션의 빈 폴더 (자료가 남은 폴더는 그대로 둔다)
            except OSError:
                pass
        db.rollback()
        if isinstance(e, Superseded):
            db.refresh(intake)  # 이 저장의 선점은 이미 없다. 다른 쪽의 상태를 건드리지 않는다
            raise
        _release(db, intake_id, token, PLAN_CHANGED if isinstance(e, NotReady) else APPLY_FAILED)
        db.refresh(intake)
        if isinstance(e, NotReady):
            raise
        if isinstance(e, Exception):
            log.exception("올리기 묶음 %s 저장 실패", intake_id)
            raise ApplyFailed(APPLY_FAILED) from e
        raise
    try:
        _sync_knowledge(db, mirror, run)
    except Exception:  # noqa: BLE001 - 기록은 이미 저장됐다. 지식 사본은 `rebuild-knowledge` 로 다시 만들 수 있다
        log.exception("올리기 묶음 %s 지식 사본 갱신 실패", intake_id)
    finally:
        remove_workdir(settings, intake)
    return run.result
