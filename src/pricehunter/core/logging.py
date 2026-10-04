import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import structlog


@contextmanager
def correlation_context(**values: object) -> Iterator[None]:
    """A top-level operation gets a fresh context, restored even on cancellation."""
    previous = structlog.contextvars.get_contextvars()
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(**values)
    try:
        yield
    finally:
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(**previous)


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
                "level": record.levelname.lower(),
                "logger": record.name,
                "event": record.getMessage(),
                **{
                    key: value
                    for key, value in structlog.contextvars.get_contextvars().items()
                    if key in {"request_id", "telegram_update_id", "job_id", "operation"}
                },
            }
        )


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
    # The ARQ CLI installs its own handler; route it through the same formatter.
    logging.getLogger("arq").handlers.clear()
    logging.getLogger("arq").propagate = True
    # These libraries may include credential-bearing URLs in their own debug logs.
    for name in ("httpx", "httpcore", "aiogram.event", "sqlalchemy.engine"):
        logging.getLogger(name).setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level)),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
