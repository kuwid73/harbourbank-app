"""The fraud rules: simple, synthetic and explainable.

Each rule that matches adds points and a reason. A transfer is blocked when its score reaches
the block score. The thresholds come from the environment, so a change to them is a reviewed
GitOps change like any other.
"""
from dataclasses import dataclass, field

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Rules(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FRAUD_")

    # A large first payment to someone the customer has never paid: the classic scam pattern
    large_new_payee_cents: int = Field(default=1_000_00, gt=0)
    # Many transfers in an hour: a taken-over account being emptied
    max_transfers_per_hour: int = Field(default=10, gt=0)
    # Large amounts in general deserve a second look
    large_amount_cents: int = Field(default=5_000_00, gt=0)
    block_score: int = Field(default=70, ge=1, le=100)


@dataclass
class Assessment:
    decision: str                  # "allow" or "block"
    score: int                     # 0-100
    reasons: list[str] = field(default_factory=list)


def assess(rules: Rules, *, amount_cents: int, first_payment_to_payee: bool, transfers_last_hour: int) -> Assessment:
    score, reasons = 0, []
    if first_payment_to_payee and amount_cents >= rules.large_new_payee_cents:
        score += 70
        reasons.append("large_first_payment_to_new_payee")
    if transfers_last_hour >= rules.max_transfers_per_hour:
        score += 70
        reasons.append("too_many_transfers_in_an_hour")
    if amount_cents >= rules.large_amount_cents:
        score += 40
        reasons.append("large_amount")
    score = min(score, 100)
    return Assessment("block" if score >= rules.block_score else "allow", score, reasons)
