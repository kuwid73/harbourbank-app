"""Prometheus metrics.

RED metrics (rate, errors, duration) per route, business metrics for transfers,
and app_info so the agent can correlate a regression with the version that shipped it.

The end-of-day job finishes in seconds, too quickly for Prometheus to scrape, so it records
its results in eod_runs and the API publishes the latest ones (bank_eod_*, bank_ledger_*).
"""
import calendar
import time

from prometheus_client import REGISTRY, Counter, Gauge, Histogram
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import SQLAlchemyError

from .config import Settings
from .models import EodRun, EodStatus

HTTP_REQUESTS = Counter(
    "http_requests_total", "HTTP requests handled", ["method", "route", "status"]
)
HTTP_DURATION = Histogram(
    "http_request_duration_seconds", "HTTP request latency", ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
TRANSFERS = Counter(
    "bank_transfers_total", "Money movements by outcome", ["kind", "status", "reason"]
)
APP_INFO = Gauge("app_info", "Running application version (always 1)", ["version", "git_sha"])
DB_POOL_SIZE = Gauge("db_pool_size", "Configured database connection pool size")
DB_POOL_IN_USE = Gauge("db_pool_connections_in_use", "Database connections currently checked out")




class EndOfDayCollector(Collector):
    """Reads the latest end-of-day results at most every CACHE_SECONDS, so scrapes stay cheap."""
    CACHE_SECONDS = 30

    def __init__(self):
        self.use(None)

    def use(self, engine: Engine | None) -> None:
        self.engine = engine
        self._values: dict[str, float] = {}
        self._read_at = float("-inf")

    def collect(self):
        if self.engine is not None and time.monotonic() - self._read_at > self.CACHE_SECONDS:
            self._values = self._read()
            self._read_at = time.monotonic()
        for name, help_text in [
            ("bank_eod_last_success_timestamp_seconds", "When the end-of-day job last closed a day successfully"),
            ("bank_eod_last_business_date_timestamp_seconds", "Latest business day closed successfully"),
            ("bank_eod_interest_paid_cents", "Interest paid by the latest finished end-of-day run"),
            ("bank_ledger_imbalance_cents", "Sum of all balances at the latest end-of-day check (must be 0)"),
            ("bank_ledger_inconsistencies", "Accounts or transfers failing the latest end-of-day check (must be 0)"),
        ]:
            if name in self._values:   # absent until the first run, rather than a misleading 0
                yield GaugeMetricFamily(name, help_text, value=self._values[name])

    def _read(self) -> dict[str, float]:
        try:
            with self.engine.connect() as conn:
                last_ok = conn.execute(
                    select(func.max(EodRun.finished_at), func.max(EodRun.business_date))
                    .where(EodRun.status == EodStatus.OK)
                ).one()
                latest = conn.execute(
                    select(EodRun).where(EodRun.status != EodStatus.RUNNING)
                    .order_by(EodRun.business_date.desc()).limit(1)
                ).first()
        except SQLAlchemyError:
            return self._values   # keep the last known values; /readyz reports database trouble
        values = {}
        if last_ok[0] is not None:
            values["bank_eod_last_success_timestamp_seconds"] = last_ok[0].timestamp()
            # midnight UTC at the start of that business day
            values["bank_eod_last_business_date_timestamp_seconds"] = calendar.timegm(last_ok[1].timetuple())
        if latest is not None:
            values["bank_eod_interest_paid_cents"] = latest.interest_cents
            values["bank_ledger_imbalance_cents"] = latest.imbalance_cents
            values["bank_ledger_inconsistencies"] = latest.inconsistencies
        return values


END_OF_DAY = EndOfDayCollector()
REGISTRY.register(END_OF_DAY)


def init(settings: Settings, engine: Engine) -> None:
    APP_INFO.labels(version=settings.app_version, git_sha=settings.git_sha).set(1)
    DB_POOL_SIZE.set(settings.db_pool_size)
    DB_POOL_IN_USE.set_function(engine.pool.checkedout)
    END_OF_DAY.use(engine)


def record_transfer(transfer) -> None:
    if transfer.replayed:   # an idempotent retry is not a new money movement
        return
    reason = transfer.reject_reason.value if transfer.reject_reason else "none"
    TRANSFERS.labels(kind=transfer.kind.value, status=transfer.status.value, reason=reason).inc()
