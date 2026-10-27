"""Request and response bodies. Amounts are integer cents everywhere."""
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .models import AccountStatus, RejectReason, TransferKind, TransferStatus

EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class _FromORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class CustomerCreate(BaseModel):
    full_name: str = Field(min_length=1, max_length=200)
    email: str = Field(pattern=EMAIL_PATTERN, max_length=320)


class CustomerUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    email: str | None = Field(default=None, pattern=EMAIL_PATTERN, max_length=320)


class CustomerOut(_FromORM):
    id: uuid.UUID
    full_name: str
    email: str
    active: bool
    created_at: datetime


class AccountCreate(BaseModel):
    customer_id: uuid.UUID
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")


class AccountStatusUpdate(BaseModel):
    status: Literal["active", "frozen", "closed"]


class AccountOut(_FromORM):
    id: uuid.UUID
    account_number: str
    customer_id: uuid.UUID | None
    currency: str
    balance_cents: int
    status: AccountStatus
    opened_at: datetime
    closed_at: datetime | None


class DepositCreate(BaseModel):
    amount_cents: int = Field(gt=0)
    reference: str = Field(default="", max_length=140)


class TransferCreate(BaseModel):
    from_account_id: uuid.UUID
    to_account_id: uuid.UUID
    amount_cents: int = Field(gt=0)
    reference: str = Field(default="", max_length=140)


class ReversalCreate(BaseModel):
    reference: str = Field(default="", max_length=140)


class TransferOut(_FromORM):
    id: uuid.UUID
    kind: TransferKind
    from_account_id: uuid.UUID
    to_account_id: uuid.UUID
    amount_cents: int
    currency: str
    reference: str
    status: TransferStatus
    reject_reason: RejectReason | None
    reversal_of_id: uuid.UUID | None
    created_at: datetime


class StatementLine(BaseModel):
    transfer_id: uuid.UUID
    amount_cents: int
    balance_after_cents: int
    reference: str
    created_at: datetime
