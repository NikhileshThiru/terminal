"""Structured logging (DESIGN.md §7).

JSON output in production, pretty key-value in development. Every log line
carries the current correlation ID if one is set.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from app.core.config import get_settings
from app.core.correlation import get_correlation_id


def _add_correlation_id(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    cid = get_correlation_id()
    if cid is not None:
        event_dict["correlation_id"] = cid
    return event_dict


def configure_logging() -> None:
    settings = get_settings()
    level = getattr(logging, settings.log_level)

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        _add_correlation_id,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if settings.environment == "development":
        processors.append(structlog.dev.ConsoleRenderer(colors=True))
    else:
        processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> Any:
    """Get a structlog logger. Use module __name__ as the name."""
    return structlog.get_logger(name)
