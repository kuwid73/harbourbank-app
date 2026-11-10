"""Harbour Bank fraud-check: scores a customer transfer before the money moves.

Run:  uvicorn fraud_check.main:create_app --factory --host 0.0.0.0 --port 8000

Stateless: accounts-api sends what the rules need (the amount, whether this is the first payment
to the payee, the account's transfers in the last hour), so this service holds no customer data.
"""
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Request
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, Field
from starlette.responses import Response

from accounts_api.logging_setup import configure_logging

from .rules import Rules, assess

log = logging.getLogger("fraud_check")

# Its own registry: the image also holds accounts-api, whose metrics must not appear here
REGISTRY = CollectorRegistry()
DECISIONS = Counter("fraud_check_decisions_total", "Transfers scored, by decision", ["decision"], registry=REGISTRY)
DURATION = Histogram("fraud_check_duration_seconds", "Time to score a transfer", registry=REGISTRY,
                     buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1))
APP_INFO = Gauge("app_info", "Running application version (always 1)", ["version", "git_sha"], registry=REGISTRY)


class ScoreRequest(BaseModel):
    from_account_id: uuid.UUID
    to_account_id: uuid.UUID
    amount_cents: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    first_payment_to_payee: bool
    transfers_last_hour: int = Field(ge=0)


class ScoreResponse(BaseModel):
    decision: Literal["allow", "block"]
    score: int
    reasons: list[str]


def create_app(rules: Rules | None = None) -> FastAPI:
    rules = rules or Rules()
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    version, git_sha = os.environ.get("APP_VERSION", "dev"), os.environ.get("GIT_SHA", "unknown")
    APP_INFO.labels(version=version, git_sha=git_sha).set(1)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        log.info("fraud-check started", extra={"version": version, "git_sha": git_sha,
                                               "rules": rules.model_dump()})
        yield

    app = FastAPI(title="Harbour Bank fraud-check", version=version, lifespan=lifespan)

    @app.post("/score", response_model=ScoreResponse)
    def score(body: ScoreRequest, request: Request) -> ScoreResponse:
        start = time.perf_counter()
        result = assess(rules, amount_cents=body.amount_cents, first_payment_to_payee=body.first_payment_to_payee,
                        transfers_last_hour=body.transfers_last_hour)
        DURATION.observe(time.perf_counter() - start)
        DECISIONS.labels(result.decision).inc()
        # Account ids only: no names, numbers or emails reach this service
        log.info("transfer scored", extra={"request_id": request.headers.get("x-request-id"),
                                           "decision": result.decision, "score": result.score,
                                           "reasons": result.reasons, "amount_cents": body.amount_cents})
        return ScoreResponse(decision=result.decision, score=result.score, reasons=result.reasons)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz():   # no dependencies: ready as soon as it serves
        return {"status": "ready"}

    @app.get("/info")
    def info():
        return {"service": "fraud-check", "version": version, "git_sha": git_sha, "rules": rules.model_dump()}

    @app.get("/metrics")
    def prometheus_metrics():
        return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    return app
