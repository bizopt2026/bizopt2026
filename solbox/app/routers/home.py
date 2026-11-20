"""홈 화면: 최근 활동, 곧 닥칠 후속 조치, 단계별 솔루션 수, AI 상태."""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import labels, transcribe
from app.db import get_db
from app.main import render
from app import intake as intake_lib
from app.models import AIJob, Activity, Intake, Proposal, ResearchRun, Solution, User
from app.routers._common import newest_first
from app.security import require_user

router = APIRouter()
RECENT_LIMIT = 10
OPEN_PROPOSAL_LIMIT = 5
FOLLOW_UP_DAYS = 14
LATEST_INSIGHTS, LATEST_SUGGESTIONS = 5, 3
INTAKE_LINK_LIMIT = 5
AI_PROBLEMS = ("not_installed", "auth", "limit", "network")


def _parse_due(value) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def upcoming_follow_ups(db: Session, today: date, days: int = FOLLOW_UP_DAYS) -> list[dict]:
    """끝나지 않은 후속 조치 중 기한이 `today + days` 이내(지난 것 포함)인 것. 기한순."""
    limit = today + timedelta(days=days)
    items = []
    for a in db.scalars(select(Activity).order_by(Activity.id)):
        for index, fu in enumerate(a.follow_ups or []):
            due = _parse_due(fu.get("due") or "")
            if fu.get("done") or due is None or due > limit:
                continue
            items.append({
                "activity": a, "index": index, "task": fu.get("task", ""),
                "owner": fu.get("owner", ""), "due": due, "overdue": due < today,
            })
    return sorted(items, key=lambda i: (i["due"], i["activity"].id, i["index"]))


def stage_counts(db: Session) -> list[tuple[str, str, int]]:
    counts = dict(db.execute(select(Solution.stage, func.count()).group_by(Solution.stage)).all())
    return [(code, name, counts.get(code, 0)) for code, name in labels.STAGES.items()]


def ai_problem(db: Session) -> str | None:
    """가장 최근에 끝난 AI 작업이 환경 문제로 실패했으면 안내문을 돌려준다."""
    job = db.scalars(
        select(AIJob).where(AIJob.finished_at.is_not(None)).order_by(AIJob.finished_at.desc(), AIJob.id.desc())
    ).first()
    if job and job.status == "failed" and job.error_kind in AI_PROBLEMS:
        return labels.ERROR_KINDS[job.error_kind]
    return None


def open_proposals(db: Session) -> list[Proposal]:
    """아직 제안서를 끝내지 않은 제안 건, 최근 수정순."""
    stmt = select(Proposal).where(Proposal.status != "deck")
    return list(db.scalars(stmt.order_by(Proposal.updated_at.desc(), Proposal.id.desc()).limit(OPEN_PROPOSAL_LIMIT)))


def latest_research(db: Session) -> dict | None:
    """가장 최근에 마감된 조사 실행의 시사점(5개)과 발굴 방향 제안(3개). 없으면 None."""
    stmt = select(ResearchRun).where(ResearchRun.status.in_(("done", "partial")))
    run = db.scalars(stmt.order_by(ResearchRun.finished_at.desc(), ResearchRun.id.desc())).first()
    if run is None:
        return None
    done = [r for r in run.reports if r.status == "done"]
    insights = [i for r in done if r.kind == "area" for i in r.insights or []]
    insights += [i for r in done if r.kind == "suggest" for i in r.insights or []]
    suggestions = [s for r in done if r.kind == "suggest" for s in r.suggestions or []]
    return {"run": run, "insights": insights[:LATEST_INSIGHTS], "suggestions": suggestions[:LATEST_SUGGESTIONS]}


def intake_summary(db: Session, user: User) -> dict:
    """올리기 묶음 중 확인 대기(최대 5개 목록과 전체 수)와 정리 중 수. 관리자도 내 것만 (남의 묶음은 올리기 목록에서 본다)."""
    def scoped(status: str):
        return select(Intake).where(Intake.status == status, Intake.created_by_id == user.id)

    def count(status: str) -> int:
        return db.scalar(select(func.count()).select_from(scoped(status).subquery())) or 0

    ready = list(db.scalars(scoped("ready").order_by(Intake.id.desc()).limit(INTAKE_LINK_LIMIT)))
    return {"ready": ready, "ready_count": count("ready"), "analyzing_count": count("analyzing")}


@router.get("/")
def home(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    recent = newest_first(db.scalars(select(Activity)))[:RECENT_LIMIT]
    return render(
        request, "home.html",
        recent=recent, follow_ups=upcoming_follow_ups(db, date.today()),
        intake=intake_summary(db, user), max_files=intake_lib.MAX_FILES,
        max_mb=request.app.state.settings.max_upload_mb, max_note=intake_lib.MAX_NOTE,
        transcriber_ready=transcribe.get(request.app.state.settings).available(),
        stages=stage_counts(db), research=latest_research(db), proposals=open_proposals(db), ai_problem=ai_problem(db),
    )
