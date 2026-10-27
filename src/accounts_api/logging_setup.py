"""Structured JSON logging with PII masking.

One JSON object per line, so Loki (and the agent) can filter on any field.
Masking runs on the final serialised line, so PII is caught whichever field it
ended up in - even ones added later by someone who forgot the rules.
Customer names are never logged: no regex can reliably recognise a name.
"""
import json
import logging
import re
import sys
from datetime import datetime, timezone

_ACCOUNT_NUMBER = re.compile(r"\bHB\d{6}(\d{4})\b")
_EMAIL = re.compile(r"\b([A-Za-z0-9])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")

# Attributes every LogRecord has; anything else was passed via extra={...}
_STANDARD_ATTRS = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message", "asctime", "taskName",
    "color_message",   # uvicorn duplicate of msg with terminal colour codes
}


def mask_pii(text: str) -> str:
    text = _ACCOUNT_NUMBER.sub(lambda m: f"HB******{m.group(1)}", text)
    return _EMAIL.sub(lambda m: f"{m.group(1)}***@{m.group(2)}", text)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            entry.setdefault("error_type", record.exc_info[0].__name__)
            entry["exc"] = self.formatException(record.exc_info)
        return mask_pii(json.dumps(entry, default=str))


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Route uvicorn's own logs through our formatter; our middleware replaces its access log
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True
    logging.getLogger("uvicorn.access").disabled = True
