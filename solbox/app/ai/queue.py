"""DB 기반 AI 작업 대기열과 워커 스레드."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.ai import tasks
from app.ai.runner import ClaudeError, ClaudeRunner
from app.config import Settings
from app.models import AIJob

log = logging.getLogger(__name__)

IDLE_SLEEP_S = 1.0
FINISH_RETRIES = 5
FINISH_RETRY_SLEEP_S = 0.5


def enqueue(db: Session, kind: str, input: dict, requester_id: int | None, priority: int = 0) -> AIJob:
    job = AIJob(kind=kind, input=input, requester_id=requester_id, priority=priority, status="pending")
    db.add(job)
    db.commit()
    return job


def queue_position(db: Session, job: AIJob) -> int:
    if job.status != "pending":
        return 0
    ahead = db.scalar(
        select(func.count()).select_from(AIJob).where(
            AIJob.status == "pending",
            or_(
                AIJob.priority < job.priority,
                and_(AIJob.priority == job.priority, AIJob.id < job.id),
            ),
        )
    )
    return (ahead or 0) + 1


def fail_stale_running(db: Session) -> int:
    result = db.execute(
        update(AIJob).where(AIJob.status == "running").values(
            status="failed", error_kind="unknown",
            error="서버가 재시작되어 중단되었습니다.", finished_at=datetime.now(),
        )
    )
    db.commit()
    return result.rowcount


def _may_claim(db: Session, kind: str, job_input: dict) -> bool:
    guard = tasks.CLAIM_GUARDS.get(kind)
    if guard is None:
        return True
    try:
        return bool(guard(db, job_input))
    except Exception:  # noqa: BLE001 - 검사가 깨져도 작업을 막지 않는다
        log.exception("작업 집기 검사 오류 (%s)", kind)
        return True


def _claim_next(session_factory: sessionmaker[Session]) -> tuple[int, str, dict] | None:
    """가장 앞선 pending 작업을 조건부 UPDATE 로 집는다. 다른 워커가 먼저 집으면 다음 것을 시도한다.
    `CLAIM_GUARDS` 가 지금은 안 된다고 한 작업(예: 다른 워커가 받아쓰는 중이라 기다릴 음성 정리)은 건너뛰고 뒤의 작업을 집는다."""
    with session_factory() as db:
        rows = db.execute(
            select(AIJob.id, AIJob.kind, AIJob.input).where(AIJob.status == "pending").order_by(AIJob.priority, AIJob.id)
        ).all()
        for row in rows:
            job_input = dict(row.input or {})
            if not _may_claim(db, row.kind, job_input):
                db.rollback()
                continue
            claimed = db.execute(
                update(AIJob).where(AIJob.id == row.id, AIJob.status == "pending")
                .values(status="running", started_at=datetime.now())
            )
            db.commit()
            if claimed.rowcount == 1:
                return row.id, row.kind, job_input
        db.rollback()
        return None


def _finish_once(session_factory: sessionmaker[Session], job_id: int, **values) -> None:
    with session_factory() as db:
        db.execute(update(AIJob).where(AIJob.id == job_id).values(finished_at=datetime.now(), **values))
        db.commit()


def _finish(session_factory: sessionmaker[Session], job_id: int, **values) -> None:
    """결과 기록이 일시적으로(DB 잠금 등) 실패해도 몇 번 다시 시도한다. 끝내 실패해도 예외를 밖으로 내지 않는다."""
    for attempt in range(1, FINISH_RETRIES + 1):
        try:
            _finish_once(session_factory, job_id, **values)
            return
        except Exception:  # noqa: BLE001
            log.exception("AI 작업 %s 결과 기록 실패 (%s/%s)", job_id, attempt, FINISH_RETRIES)
            if attempt < FINISH_RETRIES:
                time.sleep(FINISH_RETRY_SLEEP_S)


def _after_job(session_factory: sessionmaker[Session], kind: str, job_input: dict) -> None:
    hook = tasks.AFTER_JOB.get(kind)
    if hook is None:
        return
    try:
        with session_factory() as db:
            hook(db, job_input)
    except Exception:  # noqa: BLE001 - 후처리 오류는 작업 결과에 영향을 주지 않는다
        log.exception("AI 작업 후처리 오류 (%s)", kind)


def process_next(session_factory: sessionmaker[Session], runner: ClaudeRunner, settings: Settings) -> bool:
    claimed = _claim_next(session_factory)
    if claimed is None:
        return False
    job_id, kind, job_input = claimed
    try:
        handler = tasks.HANDLERS.get(kind)
        if handler is None:
            raise ValueError(f"알 수 없는 작업 종류: {kind}")
        with session_factory() as db:
            result = handler(db, {**job_input, "_job_id": job_id}, runner, settings)
    except ClaudeError as e:
        _finish(session_factory, job_id, status="failed", error_kind=e.kind, error=e.message)
    except Exception as e:  # noqa: BLE001 - 어떤 처리기 오류도 작업 실패로 기록한다
        log.exception("AI 작업 %s 처리 중 오류", job_id)
        _finish(session_factory, job_id, status="failed", error_kind="unknown", error=str(e)[-500:])
    else:
        _finish(session_factory, job_id, status="done", result=result or "")
    _after_job(session_factory, kind, job_input)
    return True


def _worker_loop(app, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            worked = process_next(app.state.session_factory, app.state.runner, app.state.settings)
        except Exception:  # noqa: BLE001
            log.exception("AI 워커 오류")
            worked = False
        if not worked:
            stop.wait(IDLE_SLEEP_S)


def start_workers(app) -> None:
    settings: Settings = app.state.settings
    app.state.worker_threads = []
    app.state.worker_stop = threading.Event()
    if not settings.ai_worker_enabled:
        return
    for i in range(max(1, settings.ai_concurrency)):
        t = threading.Thread(
            target=_worker_loop, args=(app, app.state.worker_stop), name=f"ai-worker-{i}", daemon=True
        )
        t.start()
        app.state.worker_threads.append(t)


def stop_workers(app, join_timeout: float = 5.0) -> None:
    stop = getattr(app.state, "worker_stop", None)
    if stop is None:
        return
    stop.set()
    for t in getattr(app.state, "worker_threads", []):
        t.join(timeout=join_timeout)


from app.ai import intake_tasks  # noqa: E402,F401  (처리기 등록용)
from app.ai import proposal_tasks  # noqa: E402,F401  (처리기 등록용)
from app.ai import research_tasks  # noqa: E402,F401  (처리기 등록용)
from app.ai import system_tasks  # noqa: E402,F401  (처리기 등록용)
