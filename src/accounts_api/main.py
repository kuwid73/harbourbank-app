"""Harbour Bank accounts-api.

Run:  uvicorn accounts_api.main:create_app --factory --host 0.0.0.0 --port 8000
"""
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import exc as sa_exc
from sqlalchemy.orm import sessionmaker

from . import ledger, metrics
from .config import Settings, load_settings
from .db import make_engine
from .logging_setup import configure_logging
from .routes import router

log = logging.getLogger("accounts_api")

# Probes and scrapes are logged at DEBUG so they don't drown out real traffic
QUIET_ROUTES = {"/healthz", "/readyz", "/metrics", "/info"}

# Expected business errors -> HTTP status and a stable error code (checked in order)
LEDGER_ERRORS = [
    (ledger.NotFound, 404, "not_found"),
    (ledger.IdempotencyConflict, 409, "idempotency_conflict"),
    (ledger.Conflict, 409, "conflict"),
    (ledger.InvalidRequest, 400, "invalid_request"),
]


def _unexpected_error(exc: Exception) -> tuple[int, str]:
    if isinstance(exc, sa_exc.TimeoutError):          # waited pool_timeout for a free connection
        return 503, "db_pool_exhausted"
    if isinstance(exc, (sa_exc.OperationalError, sa_exc.InterfaceError)):
        return 503, "database_unavailable"
    return 500, "internal_error"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(settings.log_level)
    engine = make_engine(settings)
    metrics.init(settings, engine)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        log.info("accounts-api started", extra={
            "version": settings.app_version,
            "git_sha": settings.git_sha,
            "environment": settings.environment,
            "daily_transfer_limit_cents": settings.daily_transfer_limit_cents,
            "db_pool_size": settings.db_pool_size,
        })
        yield
        engine.dispose()

    app = FastAPI(title="Harbour Bank accounts-api", version=settings.app_version, lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessions = sessionmaker(engine, expire_on_commit=False)

    @app.exception_handler(ledger.LedgerError)
    async def ledger_error(request: Request, exc: ledger.LedgerError):
        status, code = next((s, c) for cls, s, c in LEDGER_ERRORS if isinstance(exc, cls))
        request.state.error_type = type(exc).__name__
        return JSONResponse({"error": code, "detail": str(exc), "request_id": request.state.request_id}, status)

    @app.middleware("http")
    async def observe(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = request_id
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:
            status, code = _unexpected_error(exc)
            request.state.error_type = type(exc).__name__
            log.exception("unhandled error", extra={"request_id": request_id, "error": code})
            response = JSONResponse({"error": code, "request_id": request_id}, status)

        duration = time.perf_counter() - start
        # Route template (/accounts/{account_id}), not the raw path, to keep metric cardinality bounded
        route = request.scope.get("route")
        route_path = route.path if route else "unmatched"
        metrics.HTTP_REQUESTS.labels(request.method, route_path, str(response.status_code)).inc()
        metrics.HTTP_DURATION.labels(request.method, route_path).observe(duration)
        response.headers["X-Request-ID"] = request_id

        if response.status_code >= 500:
            level = logging.ERROR
        elif route_path in QUIET_ROUTES:
            level = logging.DEBUG
        else:
            level = logging.INFO
        fields = {
            "request_id": request_id,
            "method": request.method,
            "route": route_path,
            "status": response.status_code,
            "duration_ms": round(duration * 1000, 1),
        }
        if error_type := getattr(request.state, "error_type", None):
            fields["error_type"] = error_type
        log.log(level, "request completed", extra=fields)
        return response

    app.include_router(router)
    return app
