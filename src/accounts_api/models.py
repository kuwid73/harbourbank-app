"""Database model for Harbour Bank's double-entry ledger.

Invariants (checked by the end-of-day job and the tests):
  * every completed transfer has exactly two ledger entries that sum to zero
  * an account's balance equals the sum of its ledger entries
  * therefore the sum of all balances is always zero (deposits come from the
    bank's internal settlement account, so money is never created from nothing)

Money is always an integer number of cents - never a float.
"""
import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Enum, ForeignKey, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class AccountKind(StrEnum):
    CUSTOMER = "customer"
    INTERNAL = "internal"   # bank-owned, e.g. settlement; may go negative


class AccountStatus(StrEnum):
    ACTIVE = "active"
    FROZEN = "frozen"
    CLOSED = "closed"


class TransferKind(StrEnum):
    TRANSFER = "transfer"   # customer to customer
    DEPOSIT = "deposit"     # settlement account to customer
    REVERSAL = "reversal"   # undoes a previous transfer


class TransferStatus(StrEnum):
    COMPLETED = "completed"
    REJECTED = "rejected"   # recorded for audit; no ledger entries
    REVERSED = "reversed"   # completed, later undone by a reversal


class RejectReason(StrEnum):
    INSUFFICIENT_FUNDS = "insufficient_funds"
    ACCOUNT_FROZEN = "account_frozen"
    ACCOUNT_CLOSED = "account_closed"
    CURRENCY_MISMATCH = "currency_mismatch"
    DAILY_LIMIT_EXCEEDED = "daily_limit_exceeded"


def _enum(cls):
    # Store enum values ("active"), not names ("ACTIVE"), as native Postgres enums
    return Enum(cls, values_callable=lambda e: [m.value for m in e], name=cls.__name__.lower())


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    full_name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320), unique=True)
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    accounts: Mapped[list["Account"]] = relationship(back_populates="customer")


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        # Last line of defence: even buggy application code cannot overdraw a customer account
        CheckConstraint("kind = 'internal' OR balance_cents >= 0", name="customer_balance_non_negative"),
        CheckConstraint("kind = 'internal' OR customer_id IS NOT NULL", name="customer_account_has_owner"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    account_number: Mapped[str] = mapped_column(String(20), unique=True)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.id"))
    kind: Mapped[AccountKind] = mapped_column(_enum(AccountKind), default=AccountKind.CUSTOMER)
    currency: Mapped[str] = mapped_column(String(3))
    balance_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[AccountStatus] = mapped_column(_enum(AccountStatus), default=AccountStatus.ACTIVE)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    customer: Mapped[Customer | None] = relationship(back_populates="accounts")


class Transfer(Base):
    __tablename__ = "transfers"
    __table_args__ = (CheckConstraint("amount_cents > 0", name="amount_positive"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # Client-supplied; a retried request with the same key returns the original result
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True)
    kind: Mapped[TransferKind] = mapped_column(_enum(TransferKind))
    from_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    to_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    reference: Mapped[str] = mapped_column(String(140), default="")
    status: Mapped[TransferStatus] = mapped_column(_enum(TransferStatus))
    reject_reason: Mapped[RejectReason | None] = mapped_column(_enum(RejectReason))
    # unique: a transfer can be reversed at most once
    reversal_of_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("transfers.id"), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    transfer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transfers.id"), index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    amount_cents: Mapped[int] = mapped_column(BigInteger)          # negative = debit, positive = credit
    balance_after_cents: Mapped[int] = mapped_column(BigInteger)   # running balance, for statements
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
