"""The call to fraud-check, made before a customer transfer moves money.

fraud-check is a dependency that can be slow or down, so the call has a short timeout and no
retry, and an outage has a deliberate fallback: transfers up to FRAUD_CHECK_FALLBACK_LIMIT_CENTS
go through marked "unavailable" (unchecked, for later review), larger ones are rejected.
A slow fraud check must not make every payment slow, and its outage must not stop small payments.
"""
import json
import logging
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass

from . import metrics
from .models import FraudCheck, RejectReason

log = logging.getLogger("accounts_api.fraud")


@dataclass
class FraudResult:
    check: FraudCheck                     # allow, block or unavailable
    reject_reason: RejectReason | None    # set when the transfer must be rejected
    score: int | None = None
    reasons: tuple[str, ...] = ()


class FraudClient:
    def __init__(self, url: str, timeout_seconds: float, fallback_limit_cents: int):
        self.url = url.rstrip("/") + "/score"
        self.timeout_seconds = timeout_seconds
        self.fallback_limit_cents = fallback_limit_cents

    def assess(self, *, from_account_id: uuid.UUID, to_account_id: uuid.UUID, amount_cents: int, currency: str,
               first_payment_to_payee: bool, transfers_last_hour: int, request_id: str | None = None) -> FraudResult:
        body = json.dumps({"from_account_id": str(from_account_id), "to_account_id": str(to_account_id),
                           "amount_cents": amount_cents, "currency": currency,
                           "first_payment_to_payee": first_payment_to_payee,
                           "transfers_last_hour": transfers_last_hour}).encode()
        request = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Content-Type": "application/json", **({"X-Request-ID": request_id} if request_id else {})})
        start = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:   # noqa: S310 (config URL)
                answer = json.loads(response.read())
            decision = answer["decision"]
            if decision not in ("allow", "block"):
                raise ValueError(f"unexpected decision {decision!r}")
        except TimeoutError:
            return self._unavailable("timeout", start, amount_cents, request_id)
        except urllib.error.URLError as exc:
            # urlopen wraps a connect timeout in URLError
            outcome = "timeout" if isinstance(exc.reason, TimeoutError) else "error"
            return self._unavailable(outcome, start, amount_cents, request_id, error=str(exc.reason))
        except (OSError, ValueError, KeyError) as exc:
            return self._unavailable("error", start, amount_cents, request_id, error=f"{type(exc).__name__}: {exc}")

        metrics.FRAUD_CHECKS.labels(outcome=decision).inc()
        metrics.FRAUD_DURATION.observe(time.perf_counter() - start)
        if decision == "block":
            return FraudResult(FraudCheck.BLOCK, RejectReason.FRAUD_SUSPECTED, answer.get("score"),
                               tuple(answer.get("reasons", ())))
        return FraudResult(FraudCheck.ALLOW, None, answer.get("score"), tuple(answer.get("reasons", ())))

    def _unavailable(self, outcome: str, start: float, amount_cents: int, request_id: str | None,
                     error: str | None = None) -> FraudResult:
        metrics.FRAUD_CHECKS.labels(outcome=outcome).inc()
        metrics.FRAUD_DURATION.observe(time.perf_counter() - start)
        allowed = amount_cents <= self.fallback_limit_cents
        log.warning("fraud check unavailable", extra={
            "request_id": request_id, "outcome": outcome, "error": error, "amount_cents": amount_cents,
            "fallback": "allowed unchecked" if allowed else "rejected",
            "fallback_limit_cents": self.fallback_limit_cents})
        return FraudResult(FraudCheck.UNAVAILABLE, None if allowed else RejectReason.FRAUD_CHECK_UNAVAILABLE)
