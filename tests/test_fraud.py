"""fraud-check, and accounts-api's use of it: blocks are recorded rejections, an outage has a
deliberate fallback, and a slow fraud check never holds locks or makes payments wait long."""
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from accounts_api.config import Settings
from accounts_api.fraud import FraudClient
from accounts_api.main import create_app
from accounts_api.models import FraudCheck, RejectReason
from fraud_check.main import ScoreRequest
from fraud_check.main import create_app as create_fraud_app
from fraud_check.rules import Rules, assess

# --- the rules ------------------------------------------------------------------------------------

RULES = Rules(large_new_payee_cents=1_000_00, max_transfers_per_hour=10, large_amount_cents=5_000_00, block_score=70)


@pytest.mark.parametrize("amount, first, last_hour, decision, reasons", [
    (50_00, True, 0, "allow", []),                                          # an everyday payment
    (1_000_00, True, 0, "block", ["large_first_payment_to_new_payee"]),     # the scam pattern
    (1_000_00, False, 0, "allow", []),                                      # same amount to a known payee
    (20_00, False, 10, "block", ["too_many_transfers_in_an_hour"]),         # an account being emptied
    (6_000_00, False, 0, "allow", ["large_amount"]),                        # large alone: noted, not blocked
    (6_000_00, True, 0, "block", ["large_first_payment_to_new_payee", "large_amount"]),
])
def test_rules(amount, first, last_hour, decision, reasons):
    result = assess(RULES, amount_cents=amount, first_payment_to_payee=first, transfers_last_hour=last_hour)
    assert (result.decision, result.reasons) == (decision, reasons)
    assert 0 <= result.score <= 100


def test_the_service_scores_and_publishes_only_its_own_metrics():
    with TestClient(create_fraud_app(RULES)) as c:
        body = {"from_account_id": str(uuid.uuid4()), "to_account_id": str(uuid.uuid4()), "amount_cents": 2_000_00,
                "currency": "USD", "first_payment_to_payee": True, "transfers_last_hour": 0}
        r = c.post("/score", json=body)
        assert r.status_code == 200 and r.json()["decision"] == "block"
        assert c.post("/score", json=body | {"amount_cents": 0}).status_code == 422
        assert c.get("/readyz").status_code == 200
        text = c.get("/metrics").text
        assert 'fraud_check_decisions_total{decision="block"}' in text
        assert "http_requests_total" not in text and "bank_" not in text   # accounts-api's stay out


# --- a fraud-check stand-in that can be slow or broken ------------------------------------------

class FakeFraudCheck:
    """A real HTTP server on a free port. Every request it gets must match fraud-check's own
    request schema, so the two services can't drift apart."""

    def __init__(self):
        self.decision, self.delay, self.status, self.requests = "allow", 0.0, 200, []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(ScoreRequest.model_validate(body))
                time.sleep(fake.delay)
                answer = json.dumps({"decision": fake.decision, "score": 90 if fake.decision == "block" else 0,
                                     "reasons": ["large_first_payment_to_new_payee"]
                                     if fake.decision == "block" else []}).encode()
                self.send_response(fake.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(answer)

            def do_GET(self):      # fraud-check's /readyz
                self.send_response(fake.status)
                self.end_headers()
                self.wfile.write(b'{"status": "ready"}')

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake():
    f = FakeFraudCheck()
    yield f
    f.close()


def _assess(client, amount=50_00):
    return client.assess(from_account_id=uuid.uuid4(), to_account_id=uuid.uuid4(), amount_cents=amount,
                         currency="USD", first_payment_to_payee=True, transfers_last_hour=0)


def test_client_allow_and_block(fake):
    client = FraudClient(fake.url, 0.3, 100_00)
    assert _assess(client).check == FraudCheck.ALLOW
    fake.decision = "block"
    result = _assess(client)
    assert (result.check, result.reject_reason) == (FraudCheck.BLOCK, RejectReason.FRAUD_SUSPECTED)


@pytest.mark.parametrize("break_it", ["slow", "http_500", "bad_answer", "down"])
def test_an_outage_lets_small_transfers_through_unchecked_and_rejects_large_ones(fake, break_it):
    if break_it == "slow":
        fake.delay = 1.0
    elif break_it == "http_500":
        fake.status = 500
    elif break_it == "bad_answer":
        fake.decision = "maybe"
    url = fake.url
    if break_it == "down":
        fake.close()
    client = FraudClient(url, 0.2, 100_00)

    start = time.perf_counter()
    small, large = _assess(client, 100_00), _assess(client, 100_01)

    assert (small.check, small.reject_reason) == (FraudCheck.UNAVAILABLE, None)
    assert (large.check, large.reject_reason) == (FraudCheck.UNAVAILABLE, RejectReason.FRAUD_CHECK_UNAVAILABLE)
    assert time.perf_counter() - start < 1.0     # each call gave up at the timeout, not after the 1 s delay
    if break_it == "down":
        fake.server = type("Closed", (), {"shutdown": lambda s: None, "server_close": lambda s: None})()


# --- accounts-api with fraud-check switched on -----------------------------------------------------

@pytest.fixture
def bank(database_url, fake):
    app = create_app(Settings(database_url=database_url, daily_transfer_limit_cents=10_000_00,
                              fraud_check_url=fake.url, fraud_check_timeout_seconds=0.2,
                              fraud_check_fallback_limit_cents=100_00))
    with TestClient(app) as c:
        yield c


def _account(c, balance_cents):
    customer = c.post("/customers", json={"full_name": "Test", "email": f"{uuid.uuid4().hex[:8]}@example.test"}).json()
    account = c.post("/accounts", json={"customer_id": customer["id"], "currency": "USD"}).json()
    if balance_cents:
        assert c.post(f"/accounts/{account['id']}/deposits", json={"amount_cents": balance_cents},
                      headers={"Idempotency-Key": str(uuid.uuid4())}).status_code == 201
    return account


def _send(c, src, dst, cents, key=None):
    return c.post("/transfers", headers={"Idempotency-Key": key or str(uuid.uuid4())},
                  json={"from_account_id": src["id"], "to_account_id": dst["id"], "amount_cents": cents})


def _balance(c, account):
    return c.get(f"/accounts/{account['id']}").json()["balance_cents"]


def test_a_blocked_transfer_is_a_recorded_rejection_and_moves_no_money(bank, fake):
    alice, bob = _account(bank, 500_00), _account(bank, 0)
    assert fake.requests == []                    # deposits aren't customer transfers: not scored
    fake.decision = "block"

    r = _send(bank, alice, bob, 300_00)

    assert r.status_code == 422
    assert (r.json()["reject_reason"], r.json()["fraud_check"]) == ("fraud_suspected", "block")
    assert (_balance(bank, alice), _balance(bank, bob)) == (500_00, 0)
    assert 'bank_fraud_checks_total{outcome="block"}' in bank.get("/metrics").text


def test_an_allowed_transfer_records_the_check_and_what_was_sent(bank, fake):
    alice, bob = _account(bank, 500_00), _account(bank, 0)
    key = str(uuid.uuid4())

    first = _send(bank, alice, bob, 50_00, key)
    retry = _send(bank, alice, bob, 50_00, key)
    second = _send(bank, alice, bob, 50_00)

    assert first.status_code == 201 and first.json()["fraud_check"] == "allow"
    assert retry.headers["Idempotent-Replayed"] == "true"
    assert len(fake.requests) == 2                # the retry wasn't scored again
    assert (fake.requests[0].first_payment_to_payee, fake.requests[0].transfers_last_hour) == (True, 0)
    assert (fake.requests[1].first_payment_to_payee, fake.requests[1].transfers_last_hour) == (False, 1)
    assert second.json()["fraud_check"] == "allow"


def test_the_banks_own_rules_come_first(bank, fake):
    alice, bob = _account(bank, 10_00), _account(bank, 0)
    fake.decision = "block"
    r = _send(bank, alice, bob, 50_00)
    assert r.json()["reject_reason"] == "insufficient_funds" and r.json()["fraud_check"] == "block"


def test_during_an_outage_small_payments_continue_unchecked(bank, fake):
    alice, bob = _account(bank, 500_00), _account(bank, 0)
    fake.delay = 1.0

    small, large = _send(bank, alice, bob, 100_00), _send(bank, alice, bob, 300_00)

    assert small.status_code == 201 and small.json()["fraud_check"] == "unavailable"
    assert large.status_code == 422
    assert (large.json()["reject_reason"], large.json()["fraud_check"]) == ("fraud_check_unavailable", "unavailable")
    assert (_balance(bank, alice), _balance(bank, bob)) == (400_00, 100_00)
    assert 'bank_fraud_checks_total{outcome="timeout"}' in bank.get("/metrics").text


def test_dependencies_reports_whether_fraud_check_is_reachable(bank, fake, database_url):
    assert bank.get("/dependencies").json() == {"fraud_check": {"status": "ok"}}

    fake.status = 503                       # up, but not ready
    assert bank.get("/dependencies").json() == {"fraud_check": {"status": "unreachable", "error": "HTTP 503"}}

    fake.close()                            # cut off, as in drill 5: connection refused
    fake.server = type("Closed", (), {"shutdown": lambda s: None, "server_close": lambda s: None})()
    r = bank.get("/dependencies")
    assert r.status_code == 200             # it informs; it is not a probe
    assert r.json()["fraud_check"]["status"] == "unreachable" and "refused" in r.json()["fraud_check"]["error"]
    assert bank.get("/readyz").status_code == 200       # readiness doesn't depend on fraud-check

    with TestClient(create_app(Settings(database_url=database_url))) as c:
        assert c.get("/dependencies").json() == {"fraud_check": {"status": "not_configured"}}


def test_without_a_url_there_is_no_fraud_check(database_url):
    with TestClient(create_app(Settings(database_url=database_url))) as c:
        alice, bob = _account(c, 500_00), _account(c, 0)
        r = _send(c, alice, bob, 50_00)
        assert r.status_code == 201 and r.json()["fraud_check"] is None
