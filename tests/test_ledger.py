import threading
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from accounts_api import ledger
from accounts_api.models import Account, AccountStatus, LedgerEntry, RejectReason, Transfer, TransferStatus

NO_LIMIT = None


def key() -> str:
    return str(uuid.uuid4())


def balance(session, account) -> int:
    session.refresh(account)
    return account.balance_cents


def assert_books_balance(session):
    report = ledger.verify_ledger(session)
    assert report.ok, report


# --- happy path -------------------------------------------------------------------

def test_transfer_moves_money_and_writes_two_entries(session, new_account):
    alice, bob = new_account(100_00), new_account()

    t = ledger.transfer(session, idempotency_key=key(), from_account_id=alice.id, to_account_id=bob.id,
                        amount_cents=40_00, daily_limit_cents=NO_LIMIT)

    assert t.status == TransferStatus.COMPLETED
    assert (balance(session, alice), balance(session, bob)) == (60_00, 40_00)
    entries = session.scalars(select(LedgerEntry).where(LedgerEntry.transfer_id == t.id)).all()
    assert sorted(e.amount_cents for e in entries) == [-40_00, 40_00]
    assert_books_balance(session)


def test_deposit_is_funded_by_settlement_account(session, new_account):
    alice = new_account(250_00)

    settlement = ledger.settlement_account(session, "USD")
    assert balance(session, settlement) == -250_00   # the bank's side of the books
    assert_books_balance(session)


# --- business rules: rejected and recorded ------------------------------------------

def test_insufficient_funds_is_rejected_and_recorded(session, new_account):
    alice, bob = new_account(10_00), new_account()

    t = ledger.transfer(session, idempotency_key=key(), from_account_id=alice.id, to_account_id=bob.id,
                        amount_cents=10_01, daily_limit_cents=NO_LIMIT)

    assert (t.status, t.reject_reason) == (TransferStatus.REJECTED, RejectReason.INSUFFICIENT_FUNDS)
    assert (balance(session, alice), balance(session, bob)) == (10_00, 0)
    assert session.scalar(select(func.count()).where(LedgerEntry.transfer_id == t.id)) == 0
    assert_books_balance(session)


@pytest.mark.parametrize("which", ["source", "target"])
@pytest.mark.parametrize("status, reason", [
    (AccountStatus.FROZEN, RejectReason.ACCOUNT_FROZEN),
    (AccountStatus.CLOSED, RejectReason.ACCOUNT_CLOSED),
])
def test_frozen_or_closed_account_is_rejected(session, new_account, which, status, reason):
    alice, bob = new_account(50_00), new_account()
    (alice if which == "source" else bob).status = status
    session.commit()

    t = ledger.transfer(session, idempotency_key=key(), from_account_id=alice.id, to_account_id=bob.id,
                        amount_cents=1_00, daily_limit_cents=NO_LIMIT)

    assert (t.status, t.reject_reason) == (TransferStatus.REJECTED, reason)
    assert balance(session, alice) == 50_00


def test_currency_mismatch_is_rejected(session, new_account):
    alice, bob = new_account(50_00, "USD"), new_account(currency="EUR")

    t = ledger.transfer(session, idempotency_key=key(), from_account_id=alice.id, to_account_id=bob.id,
                        amount_cents=1_00, daily_limit_cents=NO_LIMIT)

    assert t.reject_reason == RejectReason.CURRENCY_MISMATCH


def test_daily_limit(session, new_account):
    alice, bob = new_account(1_000_00), new_account()
    send = lambda cents: ledger.transfer(session, idempotency_key=key(), from_account_id=alice.id,
                                         to_account_id=bob.id, amount_cents=cents, daily_limit_cents=100_00)

    assert send(60_00).status == TransferStatus.COMPLETED
    assert send(40_00).status == TransferStatus.COMPLETED     # exactly at the limit
    assert send(1).reject_reason == RejectReason.DAILY_LIMIT_EXCEEDED
    assert balance(session, alice) == 900_00


@pytest.mark.parametrize("amount", [0, -5_00])
def test_non_positive_amount_is_invalid(session, new_account, amount):
    alice, bob = new_account(50_00), new_account()
    with pytest.raises(ledger.InvalidRequest):
        ledger.transfer(session, idempotency_key=key(), from_account_id=alice.id, to_account_id=bob.id,
                        amount_cents=amount, daily_limit_cents=NO_LIMIT)


def test_transfer_to_same_account_is_invalid(session, new_account):
    alice = new_account(50_00)
    with pytest.raises(ledger.InvalidRequest):
        ledger.transfer(session, idempotency_key=key(), from_account_id=alice.id, to_account_id=alice.id,
                        amount_cents=1_00, daily_limit_cents=NO_LIMIT)


def test_unknown_account_is_not_found(session, new_account):
    alice = new_account(50_00)
    with pytest.raises(ledger.NotFound):
        ledger.transfer(session, idempotency_key=key(), from_account_id=alice.id, to_account_id=uuid.uuid4(),
                        amount_cents=1_00, daily_limit_cents=NO_LIMIT)


# --- idempotency ------------------------------------------------------------------

def test_retry_with_same_key_pays_once(session, new_account):
    alice, bob = new_account(100_00), new_account()
    k = key()
    send = lambda: ledger.transfer(session, idempotency_key=k, from_account_id=alice.id, to_account_id=bob.id,
                                   amount_cents=30_00, daily_limit_cents=NO_LIMIT)

    first, retry = send(), send()

    assert first.id == retry.id
    assert balance(session, alice) == 70_00
    assert_books_balance(session)


def test_retry_of_rejected_transfer_returns_original_rejection(session, new_account):
    alice, bob = new_account(10_00), new_account()
    k = key()
    send = lambda: ledger.transfer(session, idempotency_key=k, from_account_id=alice.id, to_account_id=bob.id,
                                   amount_cents=20_00, daily_limit_cents=NO_LIMIT)

    first = send()
    ledger.deposit(session, idempotency_key=key(), account_id=alice.id, amount_cents=100_00)
    retry = send()   # same key: the outcome is fixed, even though funds are now sufficient

    assert retry.id == first.id and retry.status == TransferStatus.REJECTED


def test_same_key_for_different_request_conflicts(session, new_account):
    alice, bob = new_account(100_00), new_account()
    k = key()
    ledger.transfer(session, idempotency_key=k, from_account_id=alice.id, to_account_id=bob.id,
                    amount_cents=10_00, daily_limit_cents=NO_LIMIT)
    with pytest.raises(ledger.IdempotencyConflict):
        ledger.transfer(session, idempotency_key=k, from_account_id=alice.id, to_account_id=bob.id,
                        amount_cents=99_00, daily_limit_cents=NO_LIMIT)


# --- reversals ----------------------------------------------------------------------

def test_reversal_restores_both_balances(session, new_account):
    alice, bob = new_account(100_00), new_account()
    t = ledger.transfer(session, idempotency_key=key(), from_account_id=alice.id, to_account_id=bob.id,
                        amount_cents=25_00, daily_limit_cents=NO_LIMIT)

    r = ledger.reverse(session, idempotency_key=key(), transfer_id=t.id)

    session.refresh(t)
    assert (t.status, r.status, r.reversal_of_id) == (TransferStatus.REVERSED, TransferStatus.COMPLETED, t.id)
    assert (balance(session, alice), balance(session, bob)) == (100_00, 0)
    assert_books_balance(session)


def test_transfer_can_only_be_reversed_once(session, new_account):
    alice, bob = new_account(100_00), new_account()
    t = ledger.transfer(session, idempotency_key=key(), from_account_id=alice.id, to_account_id=bob.id,
                        amount_cents=25_00, daily_limit_cents=NO_LIMIT)
    r = ledger.reverse(session, idempotency_key=key(), transfer_id=t.id)

    with pytest.raises(ledger.InvalidRequest):
        ledger.reverse(session, idempotency_key=key(), transfer_id=t.id)
    with pytest.raises(ledger.InvalidRequest):
        ledger.reverse(session, idempotency_key=key(), transfer_id=r.id)   # reversal of a reversal
    assert_books_balance(session)


def test_reversal_is_rejected_if_recipient_already_spent_the_money(session, new_account):
    alice, bob, carol = new_account(100_00), new_account(), new_account()
    t = ledger.transfer(session, idempotency_key=key(), from_account_id=alice.id, to_account_id=bob.id,
                        amount_cents=50_00, daily_limit_cents=NO_LIMIT)
    ledger.transfer(session, idempotency_key=key(), from_account_id=bob.id, to_account_id=carol.id,
                    amount_cents=40_00, daily_limit_cents=NO_LIMIT)

    r = ledger.reverse(session, idempotency_key=key(), transfer_id=t.id)

    assert r.reject_reason == RejectReason.INSUFFICIENT_FUNDS
    session.refresh(t)
    assert t.status == TransferStatus.COMPLETED   # still reversible once funds return
    assert_books_balance(session)


# --- concurrency ----------------------------------------------------------------------

def run_concurrently(engine, n, fn):
    """Run fn(session) in n threads released at the same instant; return results."""
    barrier, results, errors = threading.Barrier(n), [None] * n, []

    def worker(i):
        with Session(engine, expire_on_commit=False) as s:
            barrier.wait()
            try:
                results[i] = fn(s)
            except Exception as e:  # surfaced below
                errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    return results


def test_concurrent_transfers_never_overdraw(engine, session, new_account):
    alice, bob = new_account(100_00), new_account()

    results = run_concurrently(engine, 10, lambda s: ledger.transfer(
        s, idempotency_key=key(), from_account_id=alice.id, to_account_id=bob.id,
        amount_cents=20_00, daily_limit_cents=NO_LIMIT))

    statuses = [r.status for r in results]
    assert statuses.count(TransferStatus.COMPLETED) == 5
    assert statuses.count(TransferStatus.REJECTED) == 5
    assert (balance(session, alice), balance(session, bob)) == (0, 100_00)
    assert_books_balance(session)


def test_concurrent_transfers_after_account_already_loaded_do_not_lose_updates(engine, session, new_account):
    # An API handler typically loads the account first (e.g. to check ownership).
    # The ledger must still use the balance read under the lock, not this stale copy.
    alice, bob = new_account(100_00), new_account()
    all_loaded = threading.Barrier(5)   # every thread holds a stale copy before any transfer starts

    def send(s):
        # Keep a reference: the session's identity map is weak, so an unreferenced object
        # would be garbage-collected and reloaded fresh, hiding the bug this test guards against
        handler_copy = s.get(Account, alice.id)
        all_loaded.wait()
        result = ledger.transfer(s, idempotency_key=key(), from_account_id=alice.id, to_account_id=bob.id,
                                 amount_cents=10_00, daily_limit_cents=NO_LIMIT)
        assert handler_copy is not None
        return result

    results = run_concurrently(engine, 5, send)

    assert all(r.status == TransferStatus.COMPLETED for r in results)
    assert (balance(session, alice), balance(session, bob)) == (50_00, 50_00)
    assert_books_balance(session)


def test_concurrent_retries_with_same_key_pay_once(engine, session, new_account):
    alice, bob = new_account(100_00), new_account()
    k = key()

    results = run_concurrently(engine, 8, lambda s: ledger.transfer(
        s, idempotency_key=k, from_account_id=alice.id, to_account_id=bob.id,
        amount_cents=30_00, daily_limit_cents=NO_LIMIT))

    assert len({r.id for r in results}) == 1
    assert balance(session, alice) == 70_00
    assert session.scalar(select(func.count()).select_from(Transfer).where(Transfer.idempotency_key == k)) == 1
    assert_books_balance(session)


def test_opposite_direction_transfers_do_not_deadlock(engine, session, new_account):
    alice, bob = new_account(100_00), new_account(100_00)
    directions = [(alice.id, bob.id), (bob.id, alice.id)] * 10
    lock = threading.Lock()

    def send(s):
        with lock:
            src, dst = directions.pop()
        return ledger.transfer(s, idempotency_key=key(), from_account_id=src, to_account_id=dst,
                               amount_cents=1_00, daily_limit_cents=NO_LIMIT)

    results = run_concurrently(engine, 20, send)

    assert all(r.status == TransferStatus.COMPLETED for r in results)
    assert (balance(session, alice), balance(session, bob)) == (100_00, 100_00)
    assert_books_balance(session)


# --- defence in depth ------------------------------------------------------------------

def test_database_refuses_negative_customer_balance_even_if_code_is_buggy(session, new_account):
    alice = new_account(10_00)
    alice.balance_cents = -1   # simulate a bug that bypasses the ledger checks
    with pytest.raises(IntegrityError, match="customer_balance_non_negative"):
        session.commit()
