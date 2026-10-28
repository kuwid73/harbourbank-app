from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families
from sqlalchemy import func, select, text, update

from accounts_api import eod, ledger, metrics
from accounts_api.config import Settings
from accounts_api.main import create_app
from accounts_api.models import Account, AccountStatus, EodRun, EodStatus, LedgerEntry, Transfer, TransferKind

RATE_BPS = 150                       # 1.50% a year
BALANCE = 3_650_000                  # $36,500.00 -> exactly 150 cents of interest a day
TODAY = datetime.now(UTC).date()
YESTERDAY = TODAY - timedelta(days=1)


def balance(session, account) -> int:
    session.refresh(account)
    return account.balance_cents


def backdate(session, account, when: datetime):
    """Pretend the account's history so far happened at `when`."""
    session.execute(update(LedgerEntry).where(LedgerEntry.account_id == account.id).values(created_at=when))
    session.commit()


def interest_paid(session) -> list[int]:
    return session.scalars(select(Transfer.amount_cents).where(Transfer.kind == TransferKind.INTEREST)).all()


def test_daily_interest_rounds_down_to_whole_cents():
    assert eod.daily_interest_cents(BALANCE, RATE_BPS) == 150
    assert eod.daily_interest_cents(BALANCE - 1, RATE_BPS) == 149
    assert eod.daily_interest_cents(24_333, RATE_BPS) == 0      # under a cent: nothing paid


def test_close_day_pays_interest_from_the_interest_account(session, new_account):
    alice, bob = new_account(BALANCE), new_account(BALANCE * 2)

    run = eod.close_day(session, TODAY, RATE_BPS)

    assert (run.status, run.accounts_credited, run.interest_cents) == (EodStatus.OK, 2, 450)
    assert (balance(session, alice), balance(session, bob)) == (BALANCE + 150, BALANCE * 2 + 300)
    # The bank's side: interest is an expense of the bank, not money from nowhere
    assert balance(session, ledger.interest_account(session, "USD")) == -450
    assert (run.imbalance_cents, run.inconsistencies) == (0, 0)
    assert ledger.verify_ledger(session).ok


def test_rerunning_a_day_does_not_pay_twice(session, new_account):
    alice = new_account(BALANCE)
    eod.close_day(session, TODAY, RATE_BPS)

    run = eod.close_day(session, TODAY, RATE_BPS)   # e.g. the Job was retried after a crash

    assert (run.status, run.attempts, run.interest_cents) == (EodStatus.OK, 2, 150)
    assert balance(session, alice) == BALANCE + 150
    assert interest_paid(session) == [150]


def test_interest_uses_the_balance_at_midnight(session, new_account):
    alice = new_account(BALANCE)
    backdate(session, alice, datetime.combine(YESTERDAY, datetime.min.time(), UTC) + timedelta(hours=12))
    # Money arriving after midnight belongs to the next business day
    ledger.deposit(session, idempotency_key="late", account_id=alice.id, amount_cents=BALANCE)

    run = eod.close_day(session, YESTERDAY, RATE_BPS)

    assert run.interest_cents == 150
    # Re-running later computes the same amount - so it replays instead of conflicting
    assert eod.close_day(session, YESTERDAY, RATE_BPS).interest_cents == 150
    assert interest_paid(session) == [150]


def test_accounts_opened_after_midnight_earn_nothing_that_day(session, new_account):
    new_account(BALANCE)   # history is all "now", i.e. after yesterday's midnight

    run = eod.close_day(session, YESTERDAY, RATE_BPS)

    assert (run.status, run.accounts_credited) == (EodStatus.OK, 0)


def test_frozen_accounts_earn_no_interest(session, new_account):
    alice = new_account(BALANCE)
    ledger.set_account_status(session, account_id=alice.id, status=AccountStatus.FROZEN)

    run = eod.close_day(session, TODAY, RATE_BPS)

    assert (run.accounts_credited, balance(session, alice)) == (0, BALANCE)


def test_broken_books_stop_the_run_before_any_interest(session, new_account):
    alice = new_account(BALANCE)
    # Simulate corruption: a balance that no longer matches its ledger entries
    session.execute(update(Account).where(Account.id == alice.id).values(balance_cents=Account.balance_cents + 1))
    session.commit()

    run = eod.close_day(session, TODAY, RATE_BPS)

    assert run.status == EodStatus.FAILED
    assert "before the run" in run.error
    assert (run.imbalance_cents, run.inconsistencies) == (1, 1)
    assert type(ledger.verify_ledger(session).balances_sum_cents) is int   # not Decimal from SUM()
    assert interest_paid(session) == []


def test_days_to_close_catches_up_in_order_from_the_last_good_day(session):
    assert eod.days_to_close(session, TODAY) == [YESTERDAY]           # first ever run

    session.add(EodRun(business_date=TODAY - timedelta(days=4), status=EodStatus.OK, started_at=datetime.now(UTC)))
    session.add(EodRun(business_date=TODAY - timedelta(days=3), status=EodStatus.FAILED, started_at=datetime.now(UTC)))
    session.commit()
    assert eod.days_to_close(session, TODAY) == [TODAY - timedelta(days=d) for d in (3, 2, 1)]

    assert len(eod.days_to_close(session, TODAY + timedelta(days=30))) == eod.MAX_DAYS_PER_RUN


def test_a_failed_day_stops_later_days_from_closing(engine, session, new_account):
    session.add(EodRun(business_date=TODAY - timedelta(days=3), status=EodStatus.OK, started_at=datetime.now(UTC)))
    session.commit()
    alice = new_account(BALANCE)
    session.execute(update(Account).where(Account.id == alice.id).values(balance_cents=Account.balance_cents + 1))
    session.commit()

    assert eod.run(engine, RATE_BPS, TODAY) == 1

    runs = session.scalars(select(EodRun.status).order_by(EodRun.business_date)).all()
    assert runs == [EodStatus.OK, EodStatus.FAILED]   # day -2 failed; day -1 was not attempted


def test_only_one_run_at_a_time(engine, session):
    with engine.connect() as other:
        other.execute(text("SELECT pg_advisory_lock(:key)"), {"key": eod.LOCK_KEY})

        assert eod.run(engine, RATE_BPS, TODAY) == 0

        other.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": eod.LOCK_KEY})
    assert session.scalar(select(func.count()).select_from(EodRun)) == 0


def test_main_refuses_to_close_a_day_that_is_not_over(monkeypatch, database_url):
    monkeypatch.setenv("DATABASE_URL", database_url)
    assert eod.main(["--date", str(TODAY)]) == 2


def scrape(client) -> dict[str, float]:
    return {sample.name: sample.value
            for family in text_string_to_metric_families(client.get("/metrics").text)
            for sample in family.samples}


def test_api_publishes_the_latest_end_of_day_results(database_url, session, new_account):
    app = create_app(Settings(database_url=database_url))
    with TestClient(app) as client:
        assert "bank_ledger_imbalance_cents" not in scrape(client)   # no run yet: absent, not a misleading 0

        new_account(BALANCE)
        backdate(session, *session.scalars(select(Account).where(Account.kind == "customer")).all(),
                 datetime.combine(YESTERDAY, datetime.min.time(), UTC))
        eod.close_day(session, YESTERDAY, RATE_BPS)
        metrics.END_OF_DAY.use(app.state.engine)   # skip the 30 s cache
        values = scrape(client)

    assert values["bank_ledger_imbalance_cents"] == 0
    assert values["bank_ledger_inconsistencies"] == 0
    assert values["bank_eod_interest_paid_cents"] == 150
    assert values["bank_eod_last_business_date_timestamp_seconds"] == \
        datetime.combine(YESTERDAY, datetime.min.time(), UTC).timestamp()
    assert values["bank_eod_last_success_timestamp_seconds"] > 0
