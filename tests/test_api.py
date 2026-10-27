import io
import json
import logging
import uuid

import pytest
from fastapi.testclient import TestClient

from accounts_api.config import Settings
from accounts_api.logging_setup import JsonFormatter, mask_pii
from accounts_api.main import create_app

DAILY_LIMIT = 500_00


@pytest.fixture(scope="module")
def client(database_url):
    app = create_app(Settings(database_url=database_url, daily_transfer_limit_cents=DAILY_LIMIT))
    with TestClient(app) as c:
        yield c


def idem() -> dict:
    return {"Idempotency-Key": str(uuid.uuid4())}


def new_account(client, balance_cents=0, currency="USD") -> dict:
    customer = client.post("/customers", json={"full_name": "Ada Lovelace",
                                               "email": f"{uuid.uuid4().hex[:8]}@example.test"}).json()
    account = client.post("/accounts", json={"customer_id": customer["id"], "currency": currency}).json()
    if balance_cents:
        r = client.post(f"/accounts/{account['id']}/deposits", json={"amount_cents": balance_cents}, headers=idem())
        assert r.status_code == 201, r.text
    return account


def send(client, src, dst, cents, headers=None):
    return client.post("/transfers", headers=headers or idem(),
                       json={"from_account_id": src["id"], "to_account_id": dst["id"], "amount_cents": cents})


def balance(client, account) -> int:
    return client.get(f"/accounts/{account['id']}").json()["balance_cents"]


# --- banking flow -----------------------------------------------------------------

def test_transfer_statement_and_reversal(client):
    alice, bob = new_account(client, 100_00), new_account(client)

    r = send(client, alice, bob, 30_00)
    assert r.status_code == 201 and r.json()["status"] == "completed"
    transfer = r.json()
    assert (balance(client, alice), balance(client, bob)) == (70_00, 30_00)

    lines = client.get(f"/accounts/{alice['id']}/statement").json()
    assert [(l["amount_cents"], l["balance_after_cents"]) for l in lines] == [(-30_00, 70_00), (100_00, 100_00)]

    r = client.post(f"/transfers/{transfer['id']}/reversal", json={}, headers=idem())
    assert r.status_code == 201 and r.json()["reversal_of_id"] == transfer["id"]
    assert client.get(f"/transfers/{transfer['id']}").json()["status"] == "reversed"
    assert (balance(client, alice), balance(client, bob)) == (100_00, 0)


def test_account_lifecycle(client):
    account = new_account(client, 10_00)
    assert client.patch(f"/accounts/{account['id']}", json={"status": "frozen"}).json()["status"] == "frozen"

    r = client.patch(f"/accounts/{account['id']}", json={"status": "closed"})
    assert r.status_code == 400 and "balance must be zero" in r.json()["detail"]

    empty = new_account(client)
    assert client.patch(f"/accounts/{empty['id']}", json={"status": "closed"}).json()["closed_at"] is not None
    customer_id = empty["customer_id"]
    assert client.delete(f"/customers/{customer_id}").json()["active"] is False


# --- transfers: idempotency and rejections -----------------------------------------

def test_transfer_requires_idempotency_key(client):
    alice, bob = new_account(client, 10_00), new_account(client)
    r = client.post("/transfers", json={"from_account_id": alice["id"], "to_account_id": bob["id"], "amount_cents": 1_00})
    assert r.status_code == 422


def test_retry_returns_original_transfer_marked_as_replay(client):
    alice, bob = new_account(client, 100_00), new_account(client)
    headers = idem()

    first, retry = send(client, alice, bob, 10_00, headers), send(client, alice, bob, 10_00, headers)

    assert first.json()["id"] == retry.json()["id"]
    assert "Idempotent-Replayed" not in first.headers and retry.headers["Idempotent-Replayed"] == "true"
    assert balance(client, alice) == 90_00


def test_reusing_key_for_different_transfer_is_409(client):
    alice, bob = new_account(client, 100_00), new_account(client)
    headers = idem()
    send(client, alice, bob, 10_00, headers)
    r = send(client, alice, bob, 99_00, headers)
    assert (r.status_code, r.json()["error"]) == (409, "idempotency_conflict")


def test_rejected_transfer_is_422_with_reason(client):
    alice, bob = new_account(client, 10_00), new_account(client)
    r = send(client, alice, bob, 20_00)
    assert r.status_code == 422
    assert (r.json()["status"], r.json()["reject_reason"]) == ("rejected", "insufficient_funds")


def test_daily_limit_comes_from_settings(client):
    alice, bob = new_account(client, 1_000_00), new_account(client)
    r = send(client, alice, bob, DAILY_LIMIT + 1)
    assert r.json()["reject_reason"] == "daily_limit_exceeded"


# --- errors -----------------------------------------------------------------------

def test_unknown_account_is_404_with_request_id(client):
    r = client.get(f"/accounts/{uuid.uuid4()}", headers={"X-Request-ID": "trace-me-123"})
    assert r.status_code == 404
    assert r.json()["error"] == "not_found" and r.json()["request_id"] == "trace-me-123"
    assert r.headers["X-Request-ID"] == "trace-me-123"


def test_duplicate_email_is_409(client):
    body = {"full_name": "Grace Hopper", "email": f"{uuid.uuid4().hex[:8]}@example.test"}
    assert client.post("/customers", json=body).status_code == 201
    r = client.post("/customers", json=body)
    assert (r.status_code, r.json()["error"]) == (409, "conflict")


# --- operations ---------------------------------------------------------------------

def test_health_and_readiness(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}


def test_metrics_expose_red_business_and_version(client):
    alice, bob = new_account(client, 10_00), new_account(client)
    send(client, alice, bob, 1_00)
    send(client, alice, bob, 99_00)   # rejected

    body = client.get("/metrics").text

    assert 'bank_transfers_total{kind="transfer",reason="none",status="completed"}' in body
    assert 'bank_transfers_total{kind="transfer",reason="insufficient_funds",status="rejected"}' in body
    assert 'http_requests_total{method="POST",route="/transfers",status="201"}' in body
    assert 'route="/accounts/{account_id}"' in body       # templated, not one series per account
    assert 'app_info{git_sha="unknown",version="dev"} 1.0' in body
    assert "db_pool_connections_in_use" in body


def test_database_down_is_503_database_unavailable():
    app = create_app(Settings(database_url="postgresql://u:p@127.0.0.1:1/bank", db_pool_timeout_seconds=1))
    with TestClient(app) as c:
        r = c.get(f"/accounts/{uuid.uuid4()}")
        assert (r.status_code, r.json()["error"]) == (503, "database_unavailable")
        assert c.get("/readyz").status_code == 503
        assert c.get("/healthz").status_code == 200   # liveness must not depend on the database


def test_pool_exhaustion_is_503_db_pool_exhausted(database_url):
    app = create_app(Settings(database_url=database_url, db_pool_size=1, db_pool_timeout_seconds=0.5))
    with TestClient(app) as c, app.state.engine.connect():   # hold the only connection
        r = c.get(f"/accounts/{uuid.uuid4()}")
        assert (r.status_code, r.json()["error"]) == (503, "db_pool_exhausted")


# --- PII ------------------------------------------------------------------------------

def test_mask_pii():
    assert mask_pii("acct HB0123456789 owner ada.lovelace@example.test") == "acct HB******6789 owner a***@example.test"


def test_logs_never_contain_full_account_number(client):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logging.getLogger("accounts_api").addHandler(handler)
    try:
        account = new_account(client)
    finally:
        logging.getLogger("accounts_api").removeHandler(handler)

    output = stream.getvalue()
    number = account["account_number"]
    assert number not in output
    assert f"HB******{number[-4:]}" in output
    opened = next(json.loads(l) for l in output.splitlines() if '"account opened"' in l)
    assert opened["currency"] == "USD" and opened["request_id"]


# --- UI -------------------------------------------------------------------------------

def test_internet_banking_page_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "Harbour Bank" in r.text


def test_info_reports_environment_and_version(database_url):
    app = create_app(Settings(database_url=database_url, environment="qa", app_version="1.2.3", git_sha="abc1234"))
    with TestClient(app) as c:
        assert c.get("/info").json() == {"service": "accounts-api", "environment": "qa",
                                         "version": "1.2.3", "git_sha": "abc1234"}


def test_find_account_by_number(client):
    account = new_account(client)
    found = client.get("/accounts", params={"account_number": account["account_number"].lower()}).json()
    assert [a["id"] for a in found] == [account["id"]]
    assert client.get("/accounts", params={"account_number": "HB0000000000"}).json() == []
