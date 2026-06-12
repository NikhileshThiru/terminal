"""Correlation IDs let us trace a single event end-to-end across the funnel.

A correlation ID is set at the entry point (HTTP request, news event, etc.)
and propagated through every log line until that work item is done.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def set_correlation_id(value: str | None) -> None:
    _correlation_id.set(value)


def new_correlation_id() -> str:
    """Generate a fresh correlation ID and set it as current."""
    cid = uuid.uuid4().hex[:16]
    set_correlation_id(cid)
    return cid
