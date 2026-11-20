"""올리기 묶음 AI 작업 처리기: 올라온 파일을 읽고 솔루션 박스에 어떻게 정리할지 제안한다. `queue` 가 이 모듈을 불러와 처리기를 등록한다."""
from __future__ import annotations

import logging
import time
from datetime import date
from types import SimpleNamespace

from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ValidationError, model_validator
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app import intake as intake_lib
from app import transcribe
from app.ai.runner import ClaudeError, ClaudeRunner
from app.ai.tasks import AFTER_JOB, CLAIM_GUARDS, HANDLERS, MEMO_CLOSE, MEMO_OPEN, ActivityDraft, load_prompt, run_json
from app.config import Settings
from app.models import Customer, Intake, IntakeFile, Partner, Solution

log = logging.getLogger(__name__)

PROGRESS_INTERVAL_S = 2.0  # 받아쓰기 진행을 DB 에 적는 최소 간격
WRITE_RETRIES = 4  # 파일 한 줄 쓰기가 DB 잠금 등으로 실패했을 때 시도 횟수
WRITE_RETRY_SLEEP_S = 1.0
TRANSCRIBE_UNEXPECTED = "받아쓰는 중 오류가 났습니다. 다시 정리를 눌러 보시고, 계속 안 되면 관리자에게 알려 주세요."
TRANSCRIBE_EMPTY = "음성에서 글을 찾지 못했습니다 (소리가 없거나 알아들을 수 없습니다)."


def _text(v) -> str:
    """글 칸: 문자열은 그대로, 숫자는 글로, 그 밖(null·목록 등)은 빈 글."""
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    return ""


def _int(v) -> int | None:
    """번호 칸: 정수, 정수인 실수, 숫자만 있는 글은 정수로. 그 밖은 None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v) if v.is_integer() else None
    if isinstance(v, str) and v.strip().isdecimal():
        return int(v.strip())
    return None


def _int_list(v) -> list[int]:
    return [n for n in map(_int, v) if n is not None] if isinstance(v, list) else []


def _str_list(v) -> list[str]:
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


Text = Annotated[str, BeforeValidator(_text)]
MaybeInt = Annotated[int | None, BeforeValidator(_int)]
IntList = Annotated[list[int], BeforeValidator(_int_list)]
StrList = Annotated[list[str], BeforeValidator(_str_list)]


def _dicts(v) -> list[dict]:
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _draft(v) -> ActivityDraft | None:
    """AI 가 준 활동 초안을 느슨하게 받아 `ActivityDraft` 로 만든다 (쓸 수 없으면 None). 다른 곳의 `ActivityDraft` 는 그대로 둔다."""
    if isinstance(v, ActivityDraft):
        return v
    if not isinstance(v, dict):
        return None
    mentions = v.get("mentions") if isinstance(v.get("mentions"), dict) else {}
    clean = {
        "kind": _text(v.get("kind")) or "meeting", "date": _text(v.get("date")) or None, "title": _text(v.get("title")),
        "attendees": _str_list(v.get("attendees")), "discussion": _str_list(v.get("discussion")),
        "decisions": _str_list(v.get("decisions")),
        "follow_ups": [{"task": _text(f.get("task")), "owner": _text(f.get("owner")), "due": _text(f.get("due"))} for f in _dicts(v.get("follow_ups"))],
        "demand_signals": [{"customer": _text(d.get("customer")), "content": _text(d.get("content"))} for d in _dicts(v.get("demand_signals"))],
        "mentions": {k: _str_list(mentions.get(k)) for k in ("solutions", "customers", "businesses", "partners")},
    }
    try:
        return ActivityDraft.model_validate(clean)
    except ValidationError:
        return None


class PlanItem(BaseModel):
    """정리안 항목의 공통 칸. AI 가 엉뚱한 값을 줘도 분석 전체가 실패하지 않게 모든 칸을 느슨하게 받는다."""

    files: IntList = []
    reason: Text = ""


class NewSolution(PlanItem):
    name: Text = ""
    summary: Text = ""
    description: Text = ""
    tags: StrList = []
    partner: Text = ""


class AddToSolution(PlanItem):
    solution_id: MaybeInt = None
    note: Text = ""


class ActivityItem(PlanItem):
    draft: Annotated[ActivityDraft | None, BeforeValidator(_draft)] = None
    raw_from: MaybeInt = None  # 원문으로 쓸 파일 번호


class NewCustomer(PlanItem):
    name: Text = ""
    industry: Text = ""
    contact: Text = ""
    memo: Text = ""


class NewPartner(PlanItem):
    name: Text = ""
    contact: Text = ""
    description: Text = ""
    memo: Text = ""


class Contact(PlanItem):
    """이미 등록된 파트너·고객에 덧붙일 연락처 (명함 등). 기존 기록을 고치지 않고 연락처 칸에 한 줄을 더한다."""

    target_type: Text = ""  # partner / customer
    target_id: MaybeInt = None
    person: Text = ""
    role: Text = ""
    phone: Text = ""
    email: Text = ""
    address: Text = ""
    note: Text = ""


class Unplaced(PlanItem):
    pass


class IntakePlanOut(BaseModel):
    solutions: list[NewSolution] = []
    additions: list[AddToSolution] = []
    activities: list[ActivityItem] = []
    customers: list[NewCustomer] = []
    partners: list[NewPartner] = []
    contacts: list[Contact] = []
    unplaced: list[Unplaced] = []

    @model_validator(mode="before")
    @classmethod
    def _tolerate(cls, data):
        """목록이 아니거나 항목이 객체가 아니면 그 항목만 버린다."""
        if not isinstance(data, dict):
            return data
        out = dict(data)
        for field, model in (("solutions", NewSolution), ("additions", AddToSolution), ("activities", ActivityItem),
                             ("customers", NewCustomer), ("partners", NewPartner), ("contacts", Contact),
                             ("unplaced", Unplaced)):
            kept = []
            for d in data.get(field) if isinstance(data.get(field), list) else []:
                if isinstance(d, model):
                    kept.append(d)
                elif isinstance(d, dict):
                    try:
                        kept.append(model.model_validate(d))
                    except ValidationError:
                        pass
            out[field] = kept
        return out


def _names(db: Session, model, with_id: bool = False) -> str:
    rows = db.scalars(select(model).order_by(model.name))
    lines = [f"- id {o.id}: {intake_lib._flat(o.name)}" if with_id else f"- {intake_lib._flat(o.name)}" for o in rows]
    return "\n".join(lines) or "(없음)"


def _drop(settings: Settings, intake: Intake) -> str:
    """결과를 버리는 경우. 묶음이 버려졌거나 저장이 끝났으면 늦게 다시 생긴 작업 폴더도 지운다."""
    if intake.status in ("discarded", "applied"):
        intake_lib.remove_workdir(settings, intake)
    return "superseded"


def _write_file(engine, file_id: int, **values) -> None:
    """파일 한 줄을 짧은 독립 트랜잭션으로 고친다 (작업의 트랜잭션을 쥐거나 다른 쓰기를 막지 않는다).
    DB 가 잠겨 있으면 몇 번 다시 시도한다 (몇십 분 받아쓴 일이 알 수 없는 실패로 끝나지 않게). 끝내 안 되면 위로 올린다."""
    for attempt in range(1, WRITE_RETRIES + 1):
        try:
            with Session(engine) as s:
                s.execute(update(IntakeFile).where(IntakeFile.id == file_id).values(**values))
                s.commit()
            return
        except OperationalError:
            log.warning("파일 %s 기록 실패 (%s/%s)", file_id, attempt, WRITE_RETRIES, exc_info=True)
            if attempt == WRITE_RETRIES:
                raise
            time.sleep(WRITE_RETRY_SLEEP_S)


def _progress_writer(engine, file_id: int, intake_id: int, job_id: int):
    """받아쓰기 진행 알림 함수. `PROGRESS_INTERVAL_S` 마다 한 번 진행률(%)을 적고, 그때 묶음이 버려졌거나 다시 정리가 시작됐으면 멈춘다."""
    last: list[float | None] = [None]

    def notify(fraction: float) -> None:
        now = time.monotonic()
        if last[0] is not None and now - last[0] < PROGRESS_INTERVAL_S:
            return
        last[0] = now
        try:
            with Session(engine) as s:
                s.execute(update(IntakeFile).where(IntakeFile.id == file_id).values(progress=max(0, min(100, int(fraction * 100)))))
                s.commit()
                row = s.execute(select(Intake.status, Intake.job_id).where(Intake.id == intake_id)).first()
        except Exception:  # noqa: BLE001 - 진행 표시는 없어도 받아쓰기는 계속한다
            log.warning("받아쓰기 진행을 기록하지 못했습니다", exc_info=True)
            return
        if row is None or row.status != "analyzing" or row.job_id != job_id:
            raise transcribe.TranscribeCancelled()

    return notify


def _current(db: Session, intake_id: int, job_id: int) -> Intake | None:
    """이 작업이 아직 이 묶음의 지금 작업이면 묶음, 아니면 None (최신 값을 다시 읽는다)."""
    db.expire_all()
    intake = db.get(Intake, intake_id)
    return intake if intake is not None and intake.status == "analyzing" and intake.job_id == job_id else None


def _stop_result(db: Session, settings: Settings, intake_id: int) -> str:
    db.rollback()
    intake = db.get(Intake, intake_id)
    return "deleted" if intake is None else _drop(settings, intake)


def _transcribe_pending(db: Session, settings: Settings, intake_id: int, job_id: int) -> str | None:
    """받아쓰기를 기다리는(또는 지난번에 실패한) 음성 파일을 하나씩 받아쓴다. None 이면 계속, 문자열이면 처리기가 그 값으로 끝낸다."""
    intake = db.get(Intake, intake_id)
    todo = [f.id for f in sorted(intake.files, key=lambda x: x.id) if f.kind == "audio" and f.read_status in ("pending_transcript", "failed")]
    if not todo:
        return None
    transcriber = transcribe.get(settings)
    model, language = transcriber.model_name(), transcriber.language_name()
    hints = intake_lib.build_hints(intake_lib.registered_names(db), intake.note or "")
    engine = db.get_bind()
    folder = intake_lib.work_dir(settings, intake)
    errors: list[str] = []
    for file_id in todo:
        intake = _current(db, intake_id, job_id)  # 파일 사이마다 묶음이 그대로인지 본다
        if intake is None:
            return _stop_result(db, settings, intake_id)
        f = db.get(IntakeFile, file_id)
        number, src, name = intake_lib.file_number(intake, f), intake_lib.stored_path(settings, f), f.original_name
        # 롤백하면 ORM 객체가 만료돼 속성을 읽을 때마다 읽기 트랜잭션이 다시 열리므로, 필요한 값은 롤백 전에 복사해 둔다
        f = SimpleNamespace(id=f.id, intake_id=f.intake_id, stored_name=f.stored_name, original_name=f.original_name, kind=f.kind)
        db.rollback()  # 오래 걸리는 동안 읽기 트랜잭션(연결)을 쥐지 않는다
        text, error = intake_lib.read_transcript_cache(settings, f, model, language), ""
        cached = text is not None
        if not cached:
            _write_file(engine, file_id, progress=0)
            try:
                text = transcribe.run(transcriber, src, hints=hints,
                                      on_progress=_progress_writer(engine, file_id, intake_id, job_id)).strip()
            except transcribe.TranscribeCancelled:
                return _stop_result(db, settings, intake_id)
            except transcribe.TranscribeError as e:
                error = str(e)
            except Exception:  # noqa: BLE001 - 엔진이 어떤 오류를 내도 이 파일만 실패로 둔다
                log.exception("음성 받아쓰기 중 예상하지 못한 오류 (파일 %s)", file_id)
                error = TRANSCRIBE_UNEXPECTED
            if not error and not text:
                error = TRANSCRIBE_EMPTY
        if _current(db, intake_id, job_id) is None:  # 받아쓰는 사이 버려졌다
            return _stop_result(db, settings, intake_id)
        if not error:
            if not cached:
                intake_lib.write_transcript_cache(settings, f, model, text, language)
            try:
                folder.mkdir(parents=True, exist_ok=True)
                intake_lib.write_audio_copy(folder, number, f, text)
            except OSError:
                log.exception("받아쓴 글 사본을 쓰지 못했습니다 (파일 %s)", file_id)
                error = "받아쓴 글을 작업 폴더에 쓰지 못했습니다. 디스크 여유 공간을 확인하세요."
        if error:
            log.warning("음성 받아쓰기 실패 (파일 %s): %s", file_id, error)
            errors.append(f"{name}: {error}")
            _write_file(engine, file_id, read_status="failed", read_error=error[:300], progress=None)
        else:
            _write_file(engine, file_id, read_status="ok", read_error=None, progress=100)
    intake = _current(db, intake_id, job_id)
    if intake is None:
        return _stop_result(db, settings, intake_id)
    folder.mkdir(parents=True, exist_ok=True)
    intake_lib.write_file_list(settings, intake)
    if not any(f.read_status == "ok" for f in intake.files):
        message = f"{intake_lib.NO_READABLE} 음성을 받아쓰지 못했습니다. " + " / ".join(errors)
        db.rollback()
        intake_lib._fail_analysis(db, intake_id, job_id, message[:500])
        return "failed"
    return None


def _may_claim_analysis(db: Session, job_input: dict) -> bool:
    """받아쓰기가 필요한 정리 작업은 다른 워커가 받아쓰는 중이면 집지 않는다 (기다리느라 워커를 붙잡지 않게)."""
    intake_id = job_input.get("intake_id")
    if not isinstance(intake_id, int) or not intake_lib.has_audio_to_transcribe(db, intake_id):
        return True
    return not transcribe._RUN_LOCK.locked()


def _analyze(db: Session, input: dict, runner: ClaudeRunner, settings: Settings) -> str:
    intake = db.get(Intake, input.get("intake_id"))
    if intake is None:
        return "deleted"
    job_id, intake_id = input.get("_job_id"), intake.id
    if intake.status != "analyzing" or intake.job_id != job_id:
        return "superseded"
    folder = intake_lib.work_dir(settings, intake)
    if not (folder / intake_lib.FILE_LIST_NAME).is_file():  # 작업 폴더가 지워졌으면 원본으로 다시 만든다
        intake_lib.prepare_workdir(settings, intake)
        if intake.status != "analyzing" or intake.job_id != job_id:  # 만드는 사이 버려졌거나 다시 정리가 시작되었다
            return _drop(settings, intake)
    stopped = _transcribe_pending(db, settings, intake_id, job_id)  # 음성은 여기서 받아쓴다 (요청 안에서는 하지 않는다)
    if stopped is not None:
        return stopped
    intake = db.get(Intake, intake_id)
    note = (intake.note or "").replace(MEMO_CLOSE, "MEMO> >>").replace(MEMO_OPEN, "<<< MEMO").strip()
    prompt = load_prompt(
        "intake_analyze", today=date.today().isoformat(), note=note or "(없음)",
        solutions=_names(db, Solution, with_id=True), customers=_names(db, Customer, with_id=True),
        partners=_names(db, Partner, with_id=True),
    )
    # 파일 도구는 작업 폴더 안으로만 허용한다 (업로드 자료가 다른 파일을 읽게 시키는 것을 막는다)
    out = run_json(db, runner, prompt, IntakePlanOut, settings, cwd=folder, file_scope=True)  # 읽기 트랜잭션은 여기서 끝난다
    intake = db.get(Intake, intake_id)
    if intake is None:
        return "deleted"
    if intake.status != "analyzing" or intake.job_id != job_id:
        return _drop(settings, intake)  # 그 사이 버려졌거나 다시 정리가 시작되었다
    plan = intake_lib.clean_plan(db, intake, out, date.today(), settings)
    saved = db.execute(
        update(Intake).where(Intake.id == intake_id, Intake.status == "analyzing", Intake.job_id == job_id)
        .values(plan=plan, status="ready", error="")
    )
    db.commit()
    return "ok" if saved.rowcount == 1 else "superseded"


def _after_analyze(db: Session, input: dict) -> None:
    """작업이 실패했는데 묶음이 정리 중으로 남아 있으면 실패로 바꾸고 이유를 적는다."""
    intake_id = input.get("intake_id")
    if isinstance(intake_id, int):
        intake_lib.finish_failed_analysis(db, intake_id)


HANDLERS["intake_analyze"] = _analyze
AFTER_JOB["intake_analyze"] = _after_analyze
CLAIM_GUARDS["intake_analyze"] = _may_claim_analysis
