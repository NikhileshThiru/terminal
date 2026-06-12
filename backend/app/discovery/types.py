"""Discovery types — the events that wake up the agent funnel (DESIGN.md §4).

A `DiscoveryEvent` is the normalized shape every ingestion source produces:
EDGAR filings, Alpaca news, RSS feeds, and the flag scanner all flatten into
the same envelope before hitting the event bus. Downstream (triage + thesis)
doesn't care where the event came from.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

SourceName = Literal["edgar", "alpaca-news", "rss", "flag-scanner"]
EventKind = Literal["filing", "news", "scan"]


class DiscoveryEvent(BaseModel):
    """A normalized event from any discovery source."""

    # `id` must be globally unique across the source. EDGAR uses accession;
    # news APIs use their own ids. Used by dedup.
    id: str
    source: SourceName
    kind: EventKind

    symbols: list[str] = Field(default_factory=list, description="Affected ticker(s).")
    headline: str
    body: str | None = None
    url: str | None = None
    published_at: datetime

    # Source-specific raw payload for downstream debugging / re-processing.
    payload: dict[str, Any] = Field(default_factory=dict)

    def short_summary(self) -> str:
        """A compact one-line description for logs."""
        syms = ",".join(self.symbols[:3]) if self.symbols else "—"
        return f"[{self.source}/{self.kind}] {syms}: {self.headline[:80]}"
