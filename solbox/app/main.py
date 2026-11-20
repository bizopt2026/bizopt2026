"""FastAPI 앱 생성."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app import labels, models, scheduler  # noqa: F401  (모델을 메타데이터에 등록)
from app.ai.queue import fail_stale_running, start_workers, stop_workers
from app.ai.runner import ClaudeRunner
from app.config import Settings
from app.db import Base, make_engine, make_session_factory
from app.knowledge import KnowledgeMirror
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


def render(request: Request, template: str, status_code: int = 200, **ctx) -> HTMLResponse:
    """템플릿을 그린다. `user`, `labels` 는 항상 넣어 준다."""
    ctx.setdefault("user", getattr(request.state, "user", None))
    ctx.setdefault("labels", labels)
    return request.app.state.templates.TemplateResponse(request, template, ctx, status_code=status_code)


def _login_required_handler(request: Request, exc: LoginRequired) -> Response:
    if request.headers.get("HX-Request"):
        return Response(status_code=401, headers={"HX-Redirect": "/login"})
    return RedirectResponse("/login", status_code=303)


def _http_error_handler(request: Request, exc: StarletteHTTPException) -> Response:
    """HTTPException 을 브라우저에는 한국어 오류 화면으로, HTMX 요청에는 짧은 글로 돌려준다."""
    message = exc.detail if isinstance(exc.detail, str) and exc.detail else "요청을 처리하지 못했습니다."
    if request.headers.get("HX-Request"):
        return PlainTextResponse(message, status_code=exc.status_code, headers=exc.headers)
    response = render(request, "error.html", status_code=exc.status_code, message=message)
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
    from app.proposals import deck_is_stale, display_title

    app.state.templates.env.globals["display_title"] = display_title
    app.state.templates.env.globals["deck_is_stale"] = deck_is_stale
    app.state.templates.env.globals["labels"] = labels  # 매크로에서도 쓴다
    app.state.templates.env.filters["md"] = render_markdown
    from app.flash import pop_flash

    app.state.templates.env.globals["pop_flash"] = pop_flash
    app.state.templates.env.globals["asset_v"] = str(max((int(f.stat().st_mtime) for f in static_dir.glob("app.*")), default=0))  # 고친 CSS·JS 를 바로 받게

    from app.routers import activities, admin, auth, businesses, customers, files, home, jobs, partners, proposal_outline, proposals, research, solutions  # 지연 임포트: 라우터가 render 를 쓴다

    app.include_router(auth.router)
    app.include_router(home.router)
    app.include_router(admin.router)
    app.include_router(partners.router)
    app.include_router(customers.router)
    app.include_router(businesses.router)
    app.include_router(solutions.router)
    app.include_router(files.router)
    app.include_router(jobs.router)
    app.include_router(activities.router)
    app.include_router(proposals.router)
    app.include_router(proposal_outline.router)
    app.include_router(research.router)
    return app
