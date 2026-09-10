"""
Structured logging (Stage 10).

Machine-parseable JSON logs, one object per line, each stamped with the request's
**correlation id** (from the Stage-0 ContextVar) — so in a log aggregator you can
pivot on `correlation_id` and see every line a single request produced, across
the gateway and (later) downstream services. That is the foundation real
distributed tracing builds on.

`configure_logging()` is idempotent (safe to call on every app construction, as
the tests do) and supports a plain-text mode for local reading.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from app.core.correlation import get_correlation_id

# Standard LogRecord attributes we DON'T want to duplicate when copying "extra".
_RESERVED = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "correlation_id": get_correlation_id(),
        }
        # Fold any structured `extra={...}` fields in (http_method, duration_ms, …).
        for k, v in record.__dict__.items():
            if k not in _RESERVED and not k.startswith("_"):
                payload[k] = v
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class _TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return f"{record.levelname:<5} [{get_correlation_id()}] {record.name}: {record.getMessage()}"


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Install a single stdout handler with the chosen formatter. Idempotent:
    clears existing handlers so repeated calls don't stack duplicates."""
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter() if fmt == "json" else _TextFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
