import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.access import OFFICE_ID, Reachable
from app.api.auth import owner_dependency
from app.api.build_preview import build_preview_router
from app.api.calendar_routes import build_calendar_callback_router
from app.api.channel_routes import (
    OAuthStates,
    build_channel_callback_router,
    build_channel_router,
)
from app.api.report_pages import build_report_page_router
from app.api.routers import build_router
from app.api.routes import build_mcp_callback_router
from app.api.sessions import LoginCodes, LoginThrottle, build_session_router
from app.artifacts import ArtifactStore
from app.calendars.store import Calendars
from app.channels.inbound import Pairing
from app.channels.inbound_office import SlackOffice, run_inbound
from app.channels.notifications import run_delivery_loop
from app.config import Settings, get_settings
from app.credentials import open_secret_store
from app.db.session import SCHEMA_REVISION, make_engine, make_session_factory
from app.kernel import EventBus, Scope, load_plugins
from app.live import LiveHub
from app.mcp.oauth import Flows
from app.orchestrator.approval_store import ApprovalStore
from app.orchestrator.dispatcher import recover_runs
from app.orchestrator.runner import RunRepository
from app.plugins import BUILTIN_PLUGINS
from app.remote_access import RemoteControl
from app.runtime.worker import RunWorker, recovery_jobs, run_recovery_loop
from app.scheduler import run_scheduler
from app.version import VERSION

logger = logging.getLogger("quarterdeck.startup")


def interface_fingerprint(web: Path) -> str:
    """A short digest of the office's page code: it changes when the code does."""
    import hashlib

    digest = hashlib.sha256()
    for path in sorted(web.rglob("*")):
        if path.is_file() and path.suffix in (".js", ".css", ".html"):
            digest.update(path.relative_to(web).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


SHUTDOWN_WAIT_SECONDS = 5.0


async def _connections_returned(engine: Any) -> None:
    """Database work cancelled mid-call goes on in its thread: wait briefly for
    those connections to come back, so none outlives the pool unclosed."""
    deadline = time.monotonic() + SHUTDOWN_WAIT_SECONDS
    checked_out = getattr(engine.pool, "checkedout", lambda: 0)
    while checked_out() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    if checked_out():
        logger.warning('{"event":"connections_still_out_at_shutdown","count":%d}', checked_out())


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or get_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(config.database_url.get_secret_value())
        application.state.engine = engine
        session_factory = make_session_factory(engine)
        repository = RunRepository(session_factory, ArtifactStore(config.workspace_root))
        approvals = ApprovalStore(session_factory)
        # The office's scope tree: shared services on the root,
        # built-in plugins in the office scope, all undone at shutdown.
        root = Scope("root")
        events = EventBus()
        root.provide("events", events)
        root.provide("settings", config)
        root.provide("session_factory", session_factory)
        office = root.child("office", "main")
        load_plugins(office, BUILTIN_PLUGINS)
        application.state.scope = root
        worker = RunWorker(config, repository, approvals, session_factory, events=events)
        application.state.worker = worker
        application.state.approvals = approvals
        application.state.repository = repository
        application.state.session_factory = session_factory
        worker.start()
        # What is written, heard as it is written, for the page.
        live = LiveHub(config.database_url.get_secret_value())
        application.state.live = live
        stop_live = asyncio.Event()
        listening = asyncio.create_task(live.run(stop_live))
        # The owner's calendars, read-only: one service for the routes
        # and the scheduler's sync.
        calendars = Calendars(session_factory, lambda: open_secret_store(config))
        application.state.calendars = calendars
        application.include_router(build_calendar_callback_router(calendars))
        # A build's website, sandboxed, by its private link.
        application.include_router(build_preview_router(session_factory, config))
        # An answer as a page of its own, by its private link.
        application.include_router(build_report_page_router(session_factory, config))
        # Scheduled tasks run only while the office is running.
        stop_scheduler = asyncio.Event()
        scheduler = asyncio.create_task(
            run_scheduler(session_factory, repository, worker, config, stop_scheduler, calendars)
        )
        # Work interrupted by a restart is reclaimed and resumed from its
        # checkpoint. Nothing here re-executes engineering work. A failed scan
        # must not stop the API from serving: the run rows are durable, so
        # recovery is retried on the next start.
        try:
            for job in recovery_jobs(repository, approvals, recover_runs(repository)):
                worker.submit(job)
        except Exception:
            logger.exception('{"event":"run_recovery_failed"}')
        # The outside addresses a phone may use, and the one-time codes that
        # sign it in; shared by the session routes and the office's own.
        reachable = Reachable(session_factory)
        application.state.reachable = reachable
        application.state.remote = (
            RemoteControl(config.remote_exchange) if config.remote_exchange else None
        )
        codes = LoginCodes()
        # One count of wrong guesses for sign-in and owner-token checks alike.
        throttle = LoginThrottle()
        application.include_router(
            build_session_router(session_factory, config, throttle=throttle, codes=codes)
        )
        oauth_states = OAuthStates()
        pairing = Pairing()
        application.include_router(
            build_channel_callback_router(session_factory, config, oauth_states)
        )
        application.include_router(
            build_channel_router(
                session_factory,
                config,
                owner_dependency(config, session_factory, throttle),
                oauth_states,
                pairing,
            )
        )
        # Work abandoned by a worker that stopped is reclaimed on a sweep, not
        # only at startup, so a crash costs one lease rather than a restart.
        stop_recovery = asyncio.Event()
        recovery = asyncio.create_task(
            run_recovery_loop(repository, approvals, worker, stop_recovery)
        )
        stop_delivery = asyncio.Event()
        delivery = asyncio.create_task(run_delivery_loop(session_factory, config, stop_delivery))
        # Owner requests from Slack (Socket Mode); idle until an app token is set.
        inbound = asyncio.create_task(
            run_inbound(
                lambda app_id: SlackOffice(
                    session_factory, repository, worker, approvals, config, app_id
                ),
                session_factory,
                pairing,
                stop_delivery,
            )
        )
        sign_ins = Flows(lambda: open_secret_store(config))
        application.include_router(build_mcp_callback_router(session_factory, config, sign_ins))
        application.include_router(
            build_router(
                session_factory,
                repository,
                worker,
                approvals,
                config,
                sign_ins,
                codes=codes,
                reachable=reachable,
                throttle=throttle,
                calendars=calendars,
                events=events,
            )
        )
        try:
            yield
        finally:
            stop_live.set()
            stop_scheduler.set()
            stop_recovery.set()
            stop_delivery.set()
            # Cancelled, not awaited: a listener still connecting must not hold the shutdown.
            listening.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listening
            await scheduler
            await recovery
            await delivery
            await inbound
            await worker.stop()
            root.dispose()
            await _connections_returned(engine)
            engine.dispose()

    application = FastAPI(title="Quarterdeck", version=VERSION, lifespan=lifespan)

    # FastAPI echoes rejected input in validation errors by default. Requests
    # can carry credentials, so errors report location and reason only.
    @application.exception_handler(RequestValidationError)
    async def validation_without_input(request: Request, error: RequestValidationError) -> Response:
        detail = [
            {"type": item.get("type"), "loc": item.get("loc"), "msg": item.get("msg")}
            for item in error.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": detail})

    web = Path(__file__).resolve().parent / "api" / "web"
    application.mount("/ui", StaticFiles(directory=web), name="ui")
    # What the browser is running is compared with this, so a page kept open
    # across an update (a phone's home-screen app) reloads into it.
    interface_build = interface_fingerprint(web)

    # Only the office's own hosts are answered: the Mac itself, and the
    # outside addresses the owner set up and the office verified. Anything else
    # (another site's name pointed here, DNS rebinding) is turned away.
    @application.middleware("http")
    async def own_hosts_only(request: Request, call_next: Any) -> Response:
        reachable = getattr(application.state, "reachable", None)
        host = request.headers.get("host", "")
        probing = request.url.path.startswith("/access/probe/")
        if reachable is not None and host and not probing and not reachable.allows_host(host):
            return JSONResponse({"detail": "host_not_allowed"}, status_code=421)
        response: Response = await call_next(request)
        return response

    # Assets change whenever the interface does, and a cached copy silently
    # hides the change from whoever is looking at it.
    @application.middleware("http")
    async def no_stale_interface(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        response.headers["X-Office-Build"] = interface_build
        if request.url.path == "/" or request.url.path.startswith("/ui/"):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response

    @application.get("/", include_in_schema=False)
    def index() -> FileResponse:
        """The office."""
        return FileResponse(web / "index.html")

    @application.get("/access/probe/{nonce}", include_in_schema=False)
    def access_probe(nonce: str) -> dict[str, str]:
        """The office checking an outside address reaches it: answered
        under any host, with this office's id and the caller's nonce only."""
        return {"office": OFFICE_ID, "nonce": nonce[:64]}

    @application.get("/health/live")
    def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/health")
    def health() -> JSONResponse:
        try:
            with application.state.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
                revision = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar()
                if revision != SCHEMA_REVISION:
                    return JSONResponse(
                        {"status": "not_ready", "database": "migration_required"}, 503
                    )
        except SQLAlchemyError:
            return JSONResponse({"status": "not_ready", "database": "unavailable"}, 503)
        return JSONResponse({"status": "ok", "database": "ok"})

    return application


app = create_app()
