"""End-of-day batch: close each business day.

  python -m accounts_api.eod                     close every unclosed day up to yesterday
  python -m accounts_api.eod --date 2026-10-27   close (or retry) one specific day

For each business day, oldest first:
  1. check the ledger; if it does not balance, stop - never post interest on broken books
  2. credit a day's interest on every active customer account's end-of-day balance
  3. check the ledger again and record the result in eod_runs (the API exposes it as metrics)

A day that fails stops the run, so days are always closed in order; the next run retries it.

Safe to re-run: each interest payment's idempotency key is "eod/<date>/interest/<account>",
and its amount comes from the balance at midnight (ledger entries before the cutoff), which
never changes afterwards. A re-run computes the same amounts and the ledger replays them
instead of paying twice.

Business days are UTC. Interest is simple daily accrual (balance x rate / 365), rounded down
to whole cents and paid daily; real banks accrue the fractions and pay monthly.
"""
import argparse
import logging
import sys
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import Engine, func, select, text
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from . import ledger
from .config import load_settings
from .db import make_engine
from .logging_setup import configure_logging
from .models import Account, AccountKind, AccountStatus, EodRun, EodStatus, LedgerEntry, TransferStatus

log = logging.getLogger("accounts_api.eod")

DAYS_PER_YEAR = 365
MAX_DAYS_PER_RUN = 7          # a long outage is caught up over several runs, oldest days first
LOCK_KEY = 0x4842_454F_44     # pg advisory lock id ("HBEOD"): one end-of-day run at a time


def days_to_close(session: Session, today: date) -> list[date]:
    """Every business day after the last successfully closed one, up to yesterday."""
    yesterday = today - timedelta(days=1)
    last_ok = session.scalar(select(func.max(EodRun.business_date)).where(EodRun.status == EodStatus.OK))
    first = last_ok + timedelta(days=1) if last_ok else yesterday   # first ever run: just yesterday
    days = []
    while first <= yesterday and len(days) < MAX_DAYS_PER_RUN:
        days.append(first)
        first += timedelta(days=1)
    return days


def end_of_day_balances(session: Session, business_date: date) -> list[tuple]:
    """(account id, balance at midnight) for every active customer account with history by then."""
    cutoff = datetime.combine(business_date + timedelta(days=1), time(), UTC)
    # The latest entry before midnight carries the balance. Order by id, not created_at: created_at
    # is the transaction's start time, while ids are assigned in commit order under the account lock.
    latest = (
        select(LedgerEntry.account_id, LedgerEntry.balance_after_cents)
        .where(LedgerEntry.created_at < cutoff)
        .order_by(LedgerEntry.account_id, LedgerEntry.id.desc())
        .ext(distinct_on(LedgerEntry.account_id))
        .subquery()
    )
    return session.execute(
        select(Account.id, latest.c.balance_after_cents)
        .join(latest, latest.c.account_id == Account.id)
        .where(Account.kind == AccountKind.CUSTOMER, Account.status == AccountStatus.ACTIVE)
        .order_by(Account.id)
    ).all()


def daily_interest_cents(balance_cents: int, rate_bps: int) -> int:
    return balance_cents * rate_bps // (10_000 * DAYS_PER_YEAR)


def close_day(session: Session, business_date: date, rate_bps: int) -> EodRun:
    run = _start(session, business_date)
    try:
        before = ledger.verify_ledger(session)
        if not before.ok:
            return _finish(session, run, before, "ledger did not balance before the run; no interest posted")

        for account_id, balance_cents in end_of_day_balances(session, business_date):
            amount = daily_interest_cents(balance_cents, rate_bps)
            if amount <= 0:
                continue
            payment = ledger.pay_interest(
                session,
                idempotency_key=f"eod/{business_date}/interest/{account_id}",
                account_id=account_id,
                amount_cents=amount,
                reference=f"Interest {business_date}",
            )
            if payment.status != TransferStatus.REJECTED:   # e.g. frozen since midnight: recorded, not paid
                run.accounts_credited += 1
                run.interest_cents += amount

        after = ledger.verify_ledger(session)
        return _finish(session, run, after, None if after.ok else "ledger did not balance after the run")
    except Exception as exc:
        session.rollback()
        run.status, run.error, run.finished_at = EodStatus.FAILED, f"{type(exc).__name__}: {exc}"[:500], _now()
        session.commit()
        raise


def _start(session: Session, business_date: date) -> EodRun:
    run = session.get(EodRun, business_date)
    if run is None:
        run = EodRun(business_date=business_date, attempts=0)
        session.add(run)
    run.attempts += 1
    run.status, run.started_at, run.finished_at, run.error = EodStatus.RUNNING, _now(), None, None
    run.accounts_credited, run.interest_cents = 0, 0
    session.commit()
    return run


def _finish(session: Session, run: EodRun, report: ledger.LedgerReport, error: str | None) -> EodRun:
    run.status = EodStatus.FAILED if error else EodStatus.OK
    run.error = error
    run.imbalance_cents, run.inconsistencies = report.balances_sum_cents, report.inconsistencies
    run.finished_at = _now()
    session.commit()
    return run


def _now() -> datetime:
    return datetime.now(UTC)


def run(engine: Engine, rate_bps: int, today: date, only: date | None = None) -> int:
    with engine.connect() as lock:
        if not lock.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY}):
            log.warning("another end-of-day run is in progress; nothing to do")
            return 0
        lock.commit()   # the lock belongs to the connection and outlives this transaction
        try:
            return _close_days(engine, rate_bps, today, only)
        finally:
            # Closing the connection only returns it to the pool, where the lock would live on
            lock.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_KEY})
            lock.commit()


def _close_days(engine: Engine, rate_bps: int, today: date, only: date | None) -> int:
    with Session(engine, expire_on_commit=False) as session:
        days = [only] if only else days_to_close(session, today)
        if not days:
            log.info("no business days to close", extra={"today": str(today)})
        for business_date in days:
            result = close_day(session, business_date, rate_bps)
            fields = {
                "business_date": str(business_date),
                "status": result.status.value,
                "attempt": result.attempts,
                "accounts_credited": result.accounts_credited,
                "interest_cents": result.interest_cents,
                "imbalance_cents": result.imbalance_cents,
                "inconsistencies": result.inconsistencies,
            }
            if result.status != EodStatus.OK:
                log.error("business day failed to close: %s", result.error, extra=fields)
                return 1
            log.info("business day closed", extra=fields)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--date", type=date.fromisoformat, help="close (or retry) this business day only")
    args = parser.parse_args(argv)

    settings = load_settings()
    configure_logging(settings.log_level)
    today = _now().date()
    if args.date and args.date >= today:
        log.error("a business day can only be closed once it is over", extra={"business_date": str(args.date)})
        return 2
    engine = make_engine(settings)
    try:
        return run(engine, settings.interest_rate_bps, today, args.date)
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
