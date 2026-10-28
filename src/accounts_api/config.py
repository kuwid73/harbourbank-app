"""Application settings, read from environment variables.

The app refuses to start on missing or invalid configuration and names the exact
variable: a misconfiguration should fail loudly at deploy time, not as errors on
the first customer request.
"""
import json
import sys
from typing import Literal

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings


def normalize_db_url(url: str) -> str:
    # CloudNativePG (and most tools) emit postgresql://; SQLAlchemy needs the driver name
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


class Settings(BaseSettings):
    database_url: str
    daily_transfer_limit_cents: int = Field(default=10_000_00, gt=0)
    # Annual interest on customer balances, in basis points (150 = 1.50%); paid by the end-of-day job
    interest_rate_bps: int = Field(default=150, ge=0, le=2_000)
    db_pool_size: int = Field(default=5, ge=1)
    db_pool_timeout_seconds: float = Field(default=5.0, gt=0)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    environment: str = "local"   # dev / qa / prod; shown in the UI and logs
    app_version: str = "dev"
    git_sha: str = "unknown"

    @field_validator("database_url")
    @classmethod
    def _normalize_db_url(cls, value: str) -> str:
        return normalize_db_url(value)


def load_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        for error in exc.errors():
            variable = str(error["loc"][0]).upper()
            if error["type"] == "missing":
                problem = f"environment variable '{variable}' is required but not set"
            else:
                problem = f"environment variable '{variable}' is invalid: {error['msg']}"
            # Logging isn't configured yet; emit the same JSON shape the app logs use
            print(json.dumps({
                "level": "CRITICAL",
                "logger": "accounts_api.config",
                "msg": f"configuration error: {problem}",
                "error_type": "ConfigError",
                "variable": variable,
            }), file=sys.stderr)
        raise SystemExit(1) from None
