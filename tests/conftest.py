import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from testcontainers.community.postgres import PostgresContainer

from accounts_api import ledger
from accounts_api.models import Customer


ALEMBIC_INI = Path(__file__).parent.parent / "alembic.ini"


def alembic_config(database_url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.attributes["database_url"] = database_url
    return cfg


@pytest.fixture(scope="session")
def database_url():
    # A real Postgres: row locks and constraints behave exactly as in production
    with PostgresContainer("postgres:17-alpine", driver="psycopg") as pg:
        url = pg.get_connection_url()
        # Build the schema with the same migrations production runs, not create_all()
        command.upgrade(alembic_config(url), "head")
        yield url


@pytest.fixture(scope="session")
def engine(database_url):
    eng = create_engine(database_url, pool_size=20)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def _clean_tables(engine):
    yield
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE ledger_entries, transfers, accounts, customers CASCADE"))


@pytest.fixture
def session(engine):
    with Session(engine, expire_on_commit=False) as s:
        yield s


@pytest.fixture
def new_account(session):
    """Factory: an active account for a fresh customer, optionally funded."""
    def make(balance_cents: int = 0, currency: str = "USD"):
        customer = Customer(full_name="Test Customer", email=f"{uuid.uuid4()}@example.test")
        session.add(customer)
        session.commit()
        account = ledger.open_account(session, customer_id=customer.id, currency=currency)
        if balance_cents:
            ledger.deposit(session, idempotency_key=str(uuid.uuid4()), account_id=account.id, amount_cents=balance_cents)
        return account
    return make
