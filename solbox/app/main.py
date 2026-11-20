"""FastAPI 앱 생성."""
from __future__ import annotations

import functools
import logging
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app import labels, models, scheduler  # noqa: F401  (모델을 메타데이터에 등록)
from app.ai.queue import fail_stale_running, start_workers, stop_workers
from app.ai.runner import ClaudeRunner
from app.config import Settings
from app.db import Base, make_engine, make_session_factory
from app.knowledge import KnowledgeMirror
from app.models import Intake
from app.intake import recover as recover_intakes
from app.intake import recover_applying
from app.research import recover_runs
from app.migrate import add_missing_columns
from app.security import LoginRequired

APP_DIR = Path(__file__).resolve().parent
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 5


def _open_log_file(settings: Settings) -> tuple[logging.Logger, RotatingFileHandler, int]:
    """앱·대기열·스케줄러 로그(`app.*`)를 data_dir/logs/app.log 에 쓰는 처리기를 붙인다."""
    log_dir = settings.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / "app.log", maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger = logging.getLogger("app")
    previous_level = logger.level
    logger.addHandler(handler)
    if logger.getEffectiveLevel() > logging.INFO:
        logger.setLevel(logging.INFO)
    return logger, handler, previous_level


def _intake_ready_count(session_factory, user) -> int:
    """메뉴의 "올리기" 배지: 내 묶음 중 확인 대기 수. 요청 세션과 따로 짧은 세션을 열어 COUNT 하나만 세고 닫는다.
    로그인 화면(사용자 없음)이거나 어떤 이유로든 읽지 못하면 0 (배지 때문에 화면이 깨지면 안 된다)."""
    if user is None:
        return 0
    try:
        with session_factory() as db:
            return db.scalar(select(func.count()).select_from(Intake).where(Intake.status == "ready", Intake.created_by_id == user.id)) or 0
    except Exception:  # noqa: BLE001
        return 0


def render(request: Request, template: str, status_code: int = 200, **ctx) -> HTMLResponse:
    """템플릿을 그린다. `user`, `labels` 는 항상 넣어 준다."""
    ctx.setdefault("user", getattr(request.state, "user", None))
    ctx.setdefault("labels", labels)
    ctx.setdefault("nav_ready_count", functools.partial(_intake_ready_count, request.app.state.session_factory, ctx["user"]))
    return request.app.state.templates.TemplateResponse(request, template, ctx, status_code=status_code)


def _login_required_handler(request: Request, exc: LoginRequired) -> Response:
    if request.headers.get("HX-Request"):
        return Response(status_code=401, headers={"HX-Redirect": "/login"})
    return RedirectResponse("/login", status_code=303)


ERROR_TITLES = {404: "항목을 찾을 수 없습니다", 403: "권한이 없습니다", 400: "요청을 확인해 주세요"}


def error_title(status_code: int) -> str:
    """오류 화면 제목. 상태 코드에 맞추고, 모르는 코드는 일반 문구."""
    return ERROR_TITLES.get(status_code, "요청을 처리하지 못했습니다")


def _http_error_handler(request: Request, exc: StarletteHTTPException) -> Response:
    """HTTPException 을 브라우저에는 한국어 오류 화면으로, HTMX 요청에는 짧은 글로 돌려준다."""
    message = exc.detail if isinstance(exc.detail, str) and exc.detail else "요청을 처리하지 못했습니다."
    if request.headers.get("HX-Request"):
        return PlainTextResponse(message, status_code=exc.status_code, headers=exc.headers)
    response = render(request, "error.html", status_code=exc.status_code, message=message, title=error_title(exc.status_code))
    for key, value in (exc.headers or {}).items():
        response.headers[key] = value
    return response


def create_app(settings: Settings) -> FastAPI:
    for d in (settings.data_dir, settings.uploads_dir, settings.knowledge_dir, settings.decks_dir):
        d.mkdir(parents=True, exist_ok=True)

    engine = make_engine(settings.db_path)
    Base.metadata.create_all(engine)
    add_missing_columns(engine, settings.db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 로그 파일은 서버가 도는 동안만 연다 (끝나면 처리기를 떼고 파일을 닫는다).
        logger, handler, previous_level = _open_log_file(settings)
        with app.state.session_factory() as db:
            fail_stale_running(db)
            recover_runs(db)
            recover_intakes(db)
            recover_applying(db)
        start_workers(app)
        scheduler.start(app)
        try:
            yield
        finally:
            scheduler.stop(app)
            stop_workers(app)
            logger.removeHandler(handler)
            handler.close()
            logger.setLevel(previous_level)

    app = FastAPI(title="솔루션 박스", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.mirror = KnowledgeMirror(settings.knowledge_dir)
    app.state.runner = ClaudeRunner(settings.claude_cmd, settings.knowledge_dir)

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        same_site="lax",
        https_only=bool(settings.ssl_certfile),
    )
    app.add_exception_handler(LoginRequired, _login_required_handler)
    app.add_exception_handler(StarletteHTTPException, _http_error_handler)

    static_dir = APP_DIR / "static"
    templates_dir = APP_DIR / "templates"
    static_dir.mkdir(exist_ok=True)
    templates_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    app.state.templates = Jinja2Templates(directory=templates_dir)
    from app.markdown_lite import render_markdown
    from app.proposals import check_items, check_summary_text, number_sources, deck_is_stale, display_title, outline_check_summary

    app.state.templates.env.globals["display_title"] = display_title
    app.state.templates.env.globals["deck_is_stale"] = deck_is_stale
    app.state.templates.env.globals["check_items"] = check_items
    app.state.templates.env.globals["number_sources"] = number_sources
    app.state.templates.env.globals["outline_check_summary"] = outline_check_summary
    app.state.templates.env.globals["check_summary_text"] = check_summary_text
    app.state.templates.env.globals["labels"] = labels  # 매크로에서도 쓴다
    app.state.templates.env.globals["wait_detail"] = labels.wait_detail
    app.state.templates.env.filters["md"] = render_markdown

    from app.routers import activities, admin, auth, businesses, customers, files, home, intake, jobs, partners, proposal_outline, proposals, research, solutions  # 지연 임포트: 라우터가 render 를 쓴다

    app.include_router(auth.router)
    app.include_router(home.router)
    app.include_router(admin.router)
    app.include_router(partners.router)
    app.include_router(customers.router)
    app.include_router(businesses.router)
    app.include_router(solutions.router)
    app.include_router(files.router)
    app.include_router(intake.router)
    app.include_router(jobs.router)
    app.include_router(activities.router)
    app.include_router(proposals.router)
    app.include_router(proposal_outline.router)
    app.include_router(research.router)
    return app
