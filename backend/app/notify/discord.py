"""Discord webhook alerts (Step 9.1, DESIGN.md §8).

Phone pings for the three moments that matter: a thesis lands, a paper
trade opens, a paper trade closes. Configured by DISCORD_WEBHOOK_URL in
.env — absent means the feature is off and no code path changes.

Resilience contract (DESIGN.md §2.4): alerts are fire-and-forget. A
Discord outage, a 429, or a malformed payload must never block or fail
the funnel — we log a warning and move on. Discord allows ~30 req/min
per webhook; we produce a few events per day, so no limiter.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger

_log = get_logger(__name__)

_TIMEOUT_SECONDS = 5.0

# Embed accent colors — mirror the frontend palette (lib/palette.ts).
_GREEN = 0x2BD576  # long / profit
_RED = 0xFF4D6D  # short / loss
_AMBER = 0xFFB224  # neutral / informational


def enabled() -> bool:
    return bool(get_settings().discord_webhook_url)


# === Embed builders (pure functions — unit-tested) ===


def build_thesis_embed(
    *,
    symbol: str,
    direction: str,
    confidence: float,
    source_bucket: str,
    reasoning: str,
    grounding_passed: bool,
    occ_symbol: str | None,
    max_risk_usd: str | None,
) -> dict[str, Any]:
    arrow = "▲" if direction == "long" else "▼"
    fields: list[dict[str, Any]] = [
        {"name": "Bucket", "value": source_bucket, "inline": True},
        {"name": "Confidence", "value": f"{confidence * 100:.0f}%", "inline": True},
        {
            "name": "Grounding",
            "value": "✓ verified" if grounding_passed else "⚠ unverified figures",
            "inline": True,
        },
    ]
    if occ_symbol:
        fields.append({"name": "Contract", "value": f"`{occ_symbol}`", "inline": True})
    if max_risk_usd:
        fields.append({"name": "Max risk", "value": f"${max_risk_usd}", "inline": True})
    return _embed(
        title=f"{arrow} New thesis · {symbol} {direction.upper()} {confidence * 100:.0f}%",
        description=_truncate(reasoning, 350),
        color=_GREEN if direction == "long" else _RED,
        fields=fields,
    )


def build_trade_open_embed(
    *,
    account_kind: str,
    underlying: str,
    occ_symbol: str,
    option_type: str,
    contracts: int,
    total_cost_usd: str,
    risk_reason: str,
) -> dict[str, Any]:
    return _embed(
        title=f"◉ Paper trade OPENED · {underlying} · {account_kind}",
        description=_truncate(risk_reason, 200),
        color=_AMBER,
        fields=[
            {"name": "Contract", "value": f"`{occ_symbol}`", "inline": True},
            {"name": "Side", "value": f"long {option_type} x {contracts}", "inline": True},
            {"name": "Cost basis", "value": f"${total_cost_usd}", "inline": True},
        ],
    )


def build_trade_close_embed(
    *,
    account_kind: str,
    underlying: str,
    occ_symbol: str,
    close_reason: str,
    total_cost_usd: str,
    realized_pnl_usd: str,
) -> dict[str, Any]:
    try:
        pnl = float(realized_pnl_usd)
    except (TypeError, ValueError):
        pnl = 0.0
    sign = "+" if pnl >= 0 else "-"
    pnl_str = f"{sign}${abs(pnl):,.2f}"
    return _embed(
        title=f"◌ Paper trade CLOSED · {underlying} · {pnl_str}",
        description=f"{account_kind} · {close_reason.replace('_', ' ')}",
        color=_GREEN if pnl >= 0 else _RED,
        fields=[
            {"name": "Contract", "value": f"`{occ_symbol}`", "inline": True},
            {"name": "Cost basis", "value": f"${total_cost_usd}", "inline": True},
            {"name": "Realized P&L", "value": pnl_str, "inline": True},
        ],
    )


def _embed(
    *, title: str, description: str, color: int, fields: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "embeds": [
            {
                "title": _truncate(title, 256),
                "description": description,
                "color": color,
                "fields": fields,
                "footer": {"text": "Terminal · paper trading only"},
                "timestamp": datetime.now(UTC).isoformat(),
            }
        ]
    }


def _truncate(s: str, max_len: int) -> str:
    if len(s) <= max_len:
        return s
    return s[: max_len - 1].rstrip() + "…"


# === Delivery ===


async def send(payload: dict[str, Any]) -> bool:
    """POST the payload to the configured webhook. Returns delivery success.

    Never raises — failures are logged and swallowed (alerts are advisory)."""
    url = get_settings().discord_webhook_url
    if not url:
        return False
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, json=payload)
        if resp.status_code >= 400:
            _log.warning(
                "discord_webhook_rejected",
                status=resp.status_code,
                body=resp.text[:200],
            )
            return False
        return True
    except Exception as e:
        _log.warning("discord_webhook_failed", error=f"{type(e).__name__}: {e}")
        return False


def fire_and_forget(payload: dict[str, Any]) -> None:
    """Schedule delivery on the running loop without awaiting it."""
    if not enabled():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop (sync context / interpreter teardown) — drop quietly.
        return
    task = loop.create_task(send(payload))
    # Retrieve the (impossible) exception so asyncio never logs
    # "Task exception was never retrieved".
    task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
