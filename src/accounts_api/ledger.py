"""Money movement for Harbour Bank's double-entry ledger.

Every operation runs in a single database transaction: either both ledger entries
and both balance updates are committed, or nothing is.

Concurrency rules:
  * accounts are locked with SELECT ... FOR UPDATE before balances are read
  * locks are always taken in sorted account-id order, so A->B and B->A
    transfers running at the same time cannot deadlock
  * the idempotency key is unique in the database, so concurrent retries of the
    same request cannot both succeed
"""
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import (
    Account,
    AccountKind,
    AccountStatus,
    Customer,
    LedgerEntry,
    RejectReason,
    Transfer,
    TransferKind,
    TransferStatus,
)

SETTLEMENT_PREFIX = "HB-SETTLE-"
INTEREST_PREFIX = "HB-INTEREST-"


class LedgerError(Exception):
    """The request cannot be processed as given (a caller error)."""


class NotFound(LedgerError):
    pass


class InvalidRequest(LedgerError):
    pass


class Conflict(LedgerError):
    """The request clashes with existing data (e.g. a duplicate email)."""


class IdempotencyConflict(Conflict):
    """The idempotency key was already used for a different request."""


@dataclass
class LedgerReport:
    entries_sum_cents: int
    balances_sum_cents: int
    mismatched_accounts: list[str] = field(default_factory=list)
    unbalanced_transfers: list[uuid.UUID] = field(default_factory=list)

    @property
    def inconsistencies(self) -> int:
        return len(self.mismatched_accounts) + len(self.unbalanced_transfers)

    @property
    def ok(self) -> bool:
        return (
            self.entries_sum_cents == 0
            and self.balances_sum_cents == 0
            and not self.mismatched_accounts
            and not self.unbalanced_transfers
        )


# --- accounts -------------------------------------------------------------------

def open_account(session: Session, *, customer_id: uuid.UUID, currency: str) -> Account:
    customer = session.get(Customer, customer_id)
    if customer is None or not customer.active:
        raise NotFound(f"customer {customer_id} not found")
    account = Account(account_number=_new_account_number(), customer_id=customer_id, currency=currency.upper())
    session.add(account)
    session.commit()
    return account


def set_account_status(session: Session, *, account_id: uuid.UUID, status: AccountStatus) -> Account:
    """Freeze, unfreeze or close an account. Locks the row so no transfer is mid-flight."""
    try:
        account = _locked(session, Account, account_id)
        if account.kind != AccountKind.CUSTOMER:
            raise InvalidRequest("internal accounts cannot be changed")
        if account.status == AccountStatus.CLOSED:
            raise InvalidRequest("closed accounts cannot be changed")
        if status == AccountStatus.CLOSED:
            if account.balance_cents != 0:
                raise InvalidRequest("account balance must be zero before closing")
            account.closed_at = datetime.now(UTC)
        account.status = status
        session.commit()
        return account
    except LedgerError:
        session.rollback()
        raise


def deactivate_customer(session: Session, *, customer_id: uuid.UUID) -> Customer:
    customer = session.get(Customer, customer_id)
    if customer is None:
        raise NotFound(f"customer {customer_id} not found")
    if any(account.status != AccountStatus.CLOSED for account in customer.accounts):
        raise InvalidRequest("close all of the customer's accounts first")
    customer.active = False
    session.commit()
    return customer


def settlement_account(session: Session, currency: str) -> Account:
    """The bank's internal account that funds deposits in the given currency."""
    return _internal_account(session, SETTLEMENT_PREFIX, currency)


def interest_account(session: Session, currency: str) -> Account:
    """The bank's internal account that pays customer interest (the bank's interest expense)."""
    return _internal_account(session, INTEREST_PREFIX, currency)


def _internal_account(session: Session, prefix: str, currency: str) -> Account:
    number = f"{prefix}{currency.upper()}"
    account = session.scalar(select(Account).where(Account.account_number == number))
    if account is not None:
        return account
    try:
        account = Account(account_number=number, kind=AccountKind.INTERNAL, currency=currency.upper())
        session.add(account)
        session.commit()
        return account
    except IntegrityError:
        # Another request created it first
        session.rollback()
        return session.scalars(select(Account).where(Account.account_number == number)).one()


def _new_account_number() -> str:
    return "HB" + "".join(secrets.choice("0123456789") for _ in range(10))


# --- money movement -------------------------------------------------------------

def deposit(
    session: Session, *, idempotency_key: str, account_id: uuid.UUID, amount_cents: int, reference: str = ""
) -> Transfer:
    target = session.get(Account, account_id)
    if target is None:
        raise NotFound(f"account {account_id} not found")
    source = settlement_account(session, target.currency)
    return _execute(
        session,
        kind=TransferKind.DEPOSIT,
        idempotency_key=idempotency_key,
        from_id=source.id,
        to_id=account_id,
        amount_cents=amount_cents,
        reference=reference,
    )


def transfer(
    session: Session,
    *,
    idempotency_key: str,
    from_account_id: uuid.UUID,
    to_account_id: uuid.UUID,
    amount_cents: int,
    daily_limit_cents: int | None,
    reference: str = "",
) -> Transfer:
    """Move money between customer accounts.

    Business rule violations (insufficient funds, frozen account, ...) do not raise:
    the transfer is recorded with status REJECTED and a reject_reason, for audit.
    """
    return _execute(
        session,
        kind=TransferKind.TRANSFER,
        idempotency_key=idempotency_key,
        from_id=from_account_id,
        to_id=to_account_id,
        amount_cents=amount_cents,
        reference=reference,
        daily_limit_cents=daily_limit_cents,
    )


def pay_interest(
    session: Session, *, idempotency_key: str, account_id: uuid.UUID, amount_cents: int, reference: str
) -> Transfer:
    target = session.get(Account, account_id)
    if target is None:
        raise NotFound(f"account {account_id} not found")
    source = interest_account(session, target.currency)
    return _execute(
        session,
        kind=TransferKind.INTEREST,
        idempotency_key=idempotency_key,
        from_id=source.id,
        to_id=account_id,
        amount_cents=amount_cents,
        reference=reference,
    )


def reverse(session: Session, *, idempotency_key: str, transfer_id: uuid.UUID, reference: str = "") -> Transfer:
    """Undo a completed transfer by moving the money back. Transfers are never deleted."""
    original = session.get(Transfer, transfer_id)
    if original is None:
        raise NotFound(f"transfer {transfer_id} not found")
    if original.kind == TransferKind.REVERSAL:
        raise InvalidRequest("a reversal cannot itself be reversed")
    return _execute(
        session,
        kind=TransferKind.REVERSAL,
        idempotency_key=idempotency_key,
        from_id=original.to_account_id,
        to_id=original.from_account_id,
        amount_cents=original.amount_cents,
        reference=reference or f"Reversal of {original.id}",
        reversal_of_id=original.id,
    )


def _execute(
    session: Session,
    *,
    kind: TransferKind,
    idempotency_key: str,
    from_id: uuid.UUID,
    to_id: uuid.UUID,
    amount_cents: int,
    reference: str,
    daily_limit_cents: int | None = None,
    reversal_of_id: uuid.UUID | None = None,
) -> Transfer:
    if amount_cents <= 0:
        raise InvalidRequest("amount must be positive")
    if from_id == to_id:
        raise InvalidRequest("source and destination accounts must differ")

    existing = _find_by_key(session, idempotency_key)
    if existing is not None:
        return _replay(existing, from_id, to_id, amount_cents)

    try:
        result = _apply(
            session, kind, idempotency_key, from_id, to_id, amount_cents, reference, daily_limit_cents, reversal_of_id
        )
        session.commit()
        return result
    except IntegrityError:
        # A concurrent request with the same idempotency key committed first
        session.rollback()
        existing = _find_by_key(session, idempotency_key)
        if existing is not None:
            return _replay(existing, from_id, to_id, amount_cents)
        raise
    except LedgerError:
        session.rollback()
        raise


def _apply(session, kind, idempotency_key, from_id, to_id, amount_cents, reference, daily_limit_cents, reversal_of_id):
    original = None
    if reversal_of_id is not None:
        # Lock the original so two different reversal requests are serialised
        original = _locked(session, Transfer, reversal_of_id)
        if original.status != TransferStatus.COMPLETED:
            raise InvalidRequest(
                f"transfer {reversal_of_id} is {original.status}, only completed transfers can be reversed"
            )

    # Lock both accounts in a fixed order (sorted ids) to avoid deadlocks
    locked = {account_id: _locked(session, Account, account_id) for account_id in sorted({from_id, to_id})}
    source, target = locked[from_id], locked[to_id]

    reason = _reject_reason(session, kind, source, target, amount_cents, daily_limit_cents)
    record = Transfer(
        idempotency_key=idempotency_key,
        kind=kind,
        from_account_id=from_id,
        to_account_id=to_id,
        amount_cents=amount_cents,
        currency=source.currency,
        reference=reference,
        status=TransferStatus.REJECTED if reason else TransferStatus.COMPLETED,
        reject_reason=reason,
        reversal_of_id=reversal_of_id,
    )
    session.add(record)
    if reason:
        return record

    session.flush()  # assigns record.id for the ledger entries
    source.balance_cents -= amount_cents
    target.balance_cents += amount_cents
    session.add_all([
        LedgerEntry(transfer_id=record.id, account_id=source.id,
                    amount_cents=-amount_cents, balance_after_cents=source.balance_cents),
        LedgerEntry(transfer_id=record.id, account_id=target.id,
                    amount_cents=amount_cents, balance_after_cents=target.balance_cents),
    ])
    if original is not None:
        original.status = TransferStatus.REVERSED
    return record


def _locked(session: Session, model, row_id: uuid.UUID):
    # populate_existing: refresh the object from the locked row. Without it, an object
    # already loaded in this session keeps its stale (pre-lock) balance - a lost update.
    row = session.scalar(
        select(model).where(model.id == row_id).with_for_update().execution_options(populate_existing=True)
    )
    if row is None:
        raise NotFound(f"{model.__tablename__.rstrip('s')} {row_id} not found")
    return row


def _reject_reason(session, kind, source: Account, target: Account, amount_cents, daily_limit_cents):
    for account in (source, target):
        if account.status == AccountStatus.CLOSED:
            return RejectReason.ACCOUNT_CLOSED
        if account.status == AccountStatus.FROZEN:
            return RejectReason.ACCOUNT_FROZEN
    if source.currency != target.currency:
        return RejectReason.CURRENCY_MISMATCH
    if source.kind == AccountKind.CUSTOMER:
        if source.balance_cents < amount_cents:
            return RejectReason.INSUFFICIENT_FUNDS
        # Safe from races: the source account row is locked, so concurrent transfers
        # from it are serialised and each sees the previous one's total
        if (
            kind == TransferKind.TRANSFER
            and daily_limit_cents is not None
            and _sent_today_cents(session, source.id) + amount_cents > daily_limit_cents
        ):
            return RejectReason.DAILY_LIMIT_EXCEEDED
    return None


def _sent_today_cents(session: Session, account_id: uuid.UUID) -> int:
    start_of_day = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return session.scalar(
        select(func.coalesce(func.sum(Transfer.amount_cents), 0)).where(
            Transfer.from_account_id == account_id,
            Transfer.kind == TransferKind.TRANSFER,
            Transfer.status.in_([TransferStatus.COMPLETED, TransferStatus.REVERSED]),
            Transfer.created_at >= start_of_day,
        )
    )


def _find_by_key(session: Session, idempotency_key: str) -> Transfer | None:
    return session.scalar(select(Transfer).where(Transfer.idempotency_key == idempotency_key))


def _replay(existing: Transfer, from_id, to_id, amount_cents) -> Transfer:
    if (existing.from_account_id, existing.to_account_id, existing.amount_cents) != (from_id, to_id, amount_cents):
        raise IdempotencyConflict(f"idempotency key {existing.idempotency_key!r} was used for a different request")
    existing.replayed = True
    return existing


# --- integrity check ------------------------------------------------------------

def verify_ledger(session: Session) -> LedgerReport:
    """Check the ledger invariants. Used by the end-of-day job and the tests."""
    entry_totals = (
        select(LedgerEntry.account_id, func.sum(LedgerEntry.amount_cents).label("total"))
        .group_by(LedgerEntry.account_id)
        .subquery()
    )
    entries_total = func.coalesce(entry_totals.c.total, 0)
    mismatched = session.scalars(
        select(Account.account_number)
        .outerjoin(entry_totals, entry_totals.c.account_id == Account.id)
        .where(Account.balance_cents != entries_total)
    ).all()
    unbalanced = session.scalars(
        select(LedgerEntry.transfer_id)
        .group_by(LedgerEntry.transfer_id)
        .having(func.sum(LedgerEntry.amount_cents) != 0)
    ).all()
    # Postgres returns SUM(bigint) as numeric (a Decimal in Python); money here is always int cents
    return LedgerReport(
        entries_sum_cents=int(session.scalar(select(func.coalesce(func.sum(LedgerEntry.amount_cents), 0)))),
        balances_sum_cents=int(session.scalar(select(func.coalesce(func.sum(Account.balance_cents), 0)))),
        mismatched_accounts=list(mismatched),
        unbalanced_transfers=list(unbalanced),
    )
