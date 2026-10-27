import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import ledger, metrics
from .models import Account, AccountStatus, Customer, LedgerEntry, Transfer, TransferStatus
from .schemas import (
    AccountCreate,
    AccountOut,
    AccountStatusUpdate,
    CustomerCreate,
    CustomerOut,
    CustomerUpdate,
    DepositCreate,
    ReversalCreate,
    StatementLine,
    TransferCreate,
    TransferOut,
)

log = logging.getLogger("accounts_api")
router = APIRouter()


def get_session(request: Request):
    with request.app.state.sessions() as session:
        yield session


Db = Annotated[Session, Depends(get_session)]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=100)]


def _get_or_404(db: Session, model, row_id: uuid.UUID):
    row = db.get(model, row_id)
    if row is None:
        raise ledger.NotFound(f"{model.__tablename__.rstrip('s')} {row_id} not found")
    return row


def _commit_unique(db: Session, message: str) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ledger.Conflict(message)


def _transfer_response(request: Request, response: Response, transfer: Transfer) -> Transfer:
    metrics.record_transfer(transfer)
    response.status_code = 422 if transfer.status == TransferStatus.REJECTED else 201
    if transfer.replayed:
        response.headers["Idempotent-Replayed"] = "true"
    log.info("transfer processed", extra={
        "request_id": request.state.request_id,
        "transfer_id": str(transfer.id),
        "kind": transfer.kind.value,
        "status": transfer.status.value,
        "reject_reason": transfer.reject_reason.value if transfer.reject_reason else None,
        "amount_cents": transfer.amount_cents,
        "replayed": transfer.replayed,
    })
    return transfer


# --- customers --------------------------------------------------------------------

@router.post("/customers", status_code=201, response_model=CustomerOut, tags=["customers"])
def create_customer(body: CustomerCreate, db: Db):
    customer = Customer(**body.model_dump())
    db.add(customer)
    _commit_unique(db, "email already registered")
    return customer


@router.get("/customers", response_model=list[CustomerOut], tags=["customers"])
def list_customers(db: Db, limit: int = Query(50, le=200), offset: int = 0):
    return db.scalars(select(Customer).order_by(Customer.created_at).limit(limit).offset(offset)).all()


@router.get("/customers/{customer_id}", response_model=CustomerOut, tags=["customers"])
def get_customer(customer_id: uuid.UUID, db: Db):
    return _get_or_404(db, Customer, customer_id)


@router.patch("/customers/{customer_id}", response_model=CustomerOut, tags=["customers"])
def update_customer(customer_id: uuid.UUID, body: CustomerUpdate, db: Db):
    customer = _get_or_404(db, Customer, customer_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(customer, field, value)
    _commit_unique(db, "email already registered")
    return customer


@router.delete("/customers/{customer_id}", response_model=CustomerOut, tags=["customers"])
def deactivate_customer(customer_id: uuid.UUID, db: Db):
    """Customers are deactivated, never deleted (audit and record-keeping)."""
    return ledger.deactivate_customer(db, customer_id=customer_id)


# --- accounts ---------------------------------------------------------------------

@router.post("/accounts", status_code=201, response_model=AccountOut, tags=["accounts"])
def open_account(body: AccountCreate, request: Request, db: Db):
    account = ledger.open_account(db, customer_id=body.customer_id, currency=body.currency)
    log.info("account opened", extra={"request_id": request.state.request_id,
                                      "account_number": account.account_number, "currency": account.currency})
    return account


@router.get("/accounts", response_model=list[AccountOut], tags=["accounts"])
def list_accounts(db: Db, customer_id: uuid.UUID | None = None, limit: int = Query(50, le=200), offset: int = 0):
    query = select(Account).where(Account.customer_id.is_not(None))
    if customer_id:
        query = query.where(Account.customer_id == customer_id)
    return db.scalars(query.order_by(Account.opened_at).limit(limit).offset(offset)).all()


@router.get("/accounts/{account_id}", response_model=AccountOut, tags=["accounts"])
def get_account(account_id: uuid.UUID, db: Db):
    return _get_or_404(db, Account, account_id)


@router.patch("/accounts/{account_id}", response_model=AccountOut, tags=["accounts"])
def change_account_status(account_id: uuid.UUID, body: AccountStatusUpdate, db: Db):
    return ledger.set_account_status(db, account_id=account_id, status=AccountStatus(body.status))


@router.post("/accounts/{account_id}/deposits", response_model=TransferOut, tags=["accounts"])
def deposit(account_id: uuid.UUID, body: DepositCreate, key: IdempotencyKey,
            request: Request, response: Response, db: Db):
    transfer = ledger.deposit(db, idempotency_key=key, account_id=account_id,
                              amount_cents=body.amount_cents, reference=body.reference)
    return _transfer_response(request, response, transfer)


@router.get("/accounts/{account_id}/statement", response_model=list[StatementLine], tags=["accounts"])
def statement(account_id: uuid.UUID, db: Db,
              from_: Annotated[datetime | None, Query(alias="from")] = None,
              to: datetime | None = None,
              limit: int = Query(100, le=500)):
    _get_or_404(db, Account, account_id)
    to = to or datetime.now(timezone.utc)
    from_ = from_ or to - timedelta(days=30)
    rows = db.execute(
        select(LedgerEntry, Transfer.reference)
        .join(Transfer, Transfer.id == LedgerEntry.transfer_id)
        .where(LedgerEntry.account_id == account_id,
               LedgerEntry.created_at >= from_, LedgerEntry.created_at < to)
        .order_by(LedgerEntry.created_at.desc(), LedgerEntry.id.desc())
        .limit(limit)
    ).all()
    return [StatementLine(transfer_id=entry.transfer_id, amount_cents=entry.amount_cents,
                          balance_after_cents=entry.balance_after_cents, reference=reference,
                          created_at=entry.created_at)
            for entry, reference in rows]


# --- transfers --------------------------------------------------------------------

@router.post("/transfers", response_model=TransferOut, tags=["transfers"],
             responses={422: {"description": "Transfer rejected (see reject_reason) or invalid body"}})
def create_transfer(body: TransferCreate, key: IdempotencyKey, request: Request, response: Response, db: Db):
    transfer = ledger.transfer(
        db, idempotency_key=key, from_account_id=body.from_account_id, to_account_id=body.to_account_id,
        amount_cents=body.amount_cents, reference=body.reference,
        daily_limit_cents=request.app.state.settings.daily_transfer_limit_cents,
    )
    return _transfer_response(request, response, transfer)


@router.get("/transfers/{transfer_id}", response_model=TransferOut, tags=["transfers"])
def get_transfer(transfer_id: uuid.UUID, db: Db):
    return _get_or_404(db, Transfer, transfer_id)


@router.post("/transfers/{transfer_id}/reversal", response_model=TransferOut, tags=["transfers"])
def reverse_transfer(transfer_id: uuid.UUID, body: ReversalCreate, key: IdempotencyKey,
                     request: Request, response: Response, db: Db):
    transfer = ledger.reverse(db, idempotency_key=key, transfer_id=transfer_id, reference=body.reference)
    return _transfer_response(request, response, transfer)


# --- operations -------------------------------------------------------------------

@router.get("/healthz", tags=["ops"])
def healthz():
    """Liveness: the process is running. Deliberately does NOT check the database -
    a database outage must not make Kubernetes restart every healthy pod."""
    return {"status": "ok"}


@router.get("/readyz", tags=["ops"])
def readyz(request: Request, response: Response):
    """Readiness: can this pod serve traffic right now (database reachable)?"""
    try:
        with request.app.state.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ready"}
    except Exception as exc:
        response.status_code = 503
        log.warning("readiness check failed", extra={"error_type": type(exc).__name__})
        return {"status": "unavailable", "error_type": type(exc).__name__}


@router.get("/metrics", tags=["ops"])
def prometheus_metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
