"""Structured JSON logging configuration — Constitution §VIII.

Every log record emits at minimum:
  transaction_id, component, timestamp (ISO-8601), level, message
"""

from __future__ import annotations

import datetime
import logging
import logging.config
import threading

UTC = datetime.UTC

# Thread-local store — operators set this before processing each record
_tls = threading.local()


def set_transaction_id(txn_id: str | None) -> None:
    """Set the transaction_id for the current thread's log context."""
    _tls.transaction_id = txn_id or ""


def get_transaction_id() -> str:
    return getattr(_tls, "transaction_id", "")


class _TransactionFilter(logging.Filter):
    """Inject transaction_id from thread-local into every LogRecord."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.transaction_id = get_transaction_id()  # type: ignore[attr-defined]
        return True


class _JsonFormatter(logging.Formatter):
    """Emit log records as single-line JSON objects."""

    import json as _json
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    def format(self, record: logging.LogRecord) -> str:
        import json
        from datetime import datetime

        self.format_exception_info(record)
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "component": record.name,
            "transaction_id": getattr(record, "transaction_id", ""),
            "message": record.getMessage(),
        }
        if record.exc_text:
            payload["exception"] = record.exc_text
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def format_exception_info(record: logging.LogRecord) -> None:
        if record.exc_info and not record.exc_text:
            record.exc_text = logging._defaultFormatter.formatException(record.exc_info)


def configure_logging(level: str = "INFO") -> None:
    """Apply structured JSON logging config. Call once from job.py entry point."""
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {
                "transaction_filter": {
                    "()": _TransactionFilter,
                }
            },
            "formatters": {
                "json": {
                    "()": _JsonFormatter,
                }
            },
            "handlers": {
                "stdout": {
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stdout",
                    "formatter": "json",
                    "filters": ["transaction_filter"],
                }
            },
            "root": {
                "level": level,
                "handlers": ["stdout"],
            },
        }
    )
