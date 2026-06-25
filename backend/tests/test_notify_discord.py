"""Tests for the Discord webhook alerter (Step 9.1).

The resilience contract is the important one here: a Discord outage must NEVER
block or fail the funnel (DESIGN.md §2.4). Most tests assert *non-raising*
behavior on broken webhooks, not just happy-path delivery.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
import respx

from app.core.config import get_settings
from app.notify import discord

_WEBHOOK = "https://discord.com/api/webhooks/123/abc"


# === enabled() / config-gating ===


def test_enabled_false_when_webhook_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # Empty string beats .env (env var has priority in pydantic-settings) and
    # is falsy, so enabled() reads "no webhook configured" regardless of what's
    # in the developer's local .env file.
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "")
    get_settings.cache_clear()
    assert discord.enabled() is False


def test_enabled_true_when_webhook_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", _WEBHOOK)
    get_settings.cache_clear()
    assert discord.enabled() is True


# === Embed builders (pure) ===


def test_build_thesis_embed_long_is_green() -> None:
    payload = discord.build_thesis_embed(
        symbol="NVDA",
        direction="long",
        confidence=0.72,
        source_bucket="reactive",
        reasoning="Beat-and-raise quarter; AI capex still expanding.",
        grounding_passed=True,
        occ_symbol="NVDA260117C00500000",
        max_risk_usd="120.00",
    )
    embed = payload["embeds"][0]
    assert embed["color"] == 0x2BD576  # _GREEN
    assert "NVDA" in embed["title"]
    assert "LONG" in embed["title"]
    assert "72%" in embed["title"]
    field_names = {f["name"] for f in embed["fields"]}
    assert {"Bucket", "Confidence", "Grounding", "Contract", "Max risk"} <= field_names


def test_build_thesis_embed_short_is_red_and_omits_optional_fields() -> None:
    payload = discord.build_thesis_embed(
        symbol="AAPL",
        direction="short",
        confidence=0.61,
        source_bucket="catalyst",
        reasoning="iPhone demand softness post-launch.",
        grounding_passed=False,
        occ_symbol=None,
        max_risk_usd=None,
    )
    embed = payload["embeds"][0]
    assert embed["color"] == 0xFF4D6D  # _RED
    field_names = {f["name"] for f in embed["fields"]}
    # Grounding flips to warning when unverified
    grounding = next(f for f in embed["fields"] if f["name"] == "Grounding")
    assert "unverified" in grounding["value"]
    # Optional fields suppressed when None
    assert "Contract" not in field_names
    assert "Max risk" not in field_names


def test_build_thesis_embed_truncates_long_reasoning() -> None:
    long_text = "x" * 1000
    payload = discord.build_thesis_embed(
        symbol="TSLA",
        direction="long",
        confidence=0.6,
        source_bucket="manual",
        reasoning=long_text,
        grounding_passed=True,
        occ_symbol=None,
        max_risk_usd=None,
    )
    desc = payload["embeds"][0]["description"]
    assert len(desc) <= 350
    assert desc.endswith("…")


def test_build_trade_open_embed_shape() -> None:
    payload = discord.build_trade_open_embed(
        account_kind="aggressive",
        underlying="MSFT",
        occ_symbol="MSFT260117C00400000",
        option_type="call",
        contracts=2,
        total_cost_usd="240.00",
        risk_reason="approved: passes confidence + budget",
    )
    embed = payload["embeds"][0]
    assert embed["color"] == 0xFFB224  # _AMBER
    assert "OPENED" in embed["title"]
    assert "aggressive" in embed["title"]
    field_names = {f["name"] for f in embed["fields"]}
    assert {"Contract", "Side", "Cost basis"} <= field_names


def test_build_trade_close_embed_positive_pnl_is_green() -> None:
    payload = discord.build_trade_close_embed(
        account_kind="conservative",
        underlying="NVDA",
        occ_symbol="NVDA260117C00500000",
        close_reason="theta_exit",
        total_cost_usd="120.00",
        realized_pnl_usd="45.50",
    )
    embed = payload["embeds"][0]
    assert embed["color"] == 0x2BD576
    assert "+$45.50" in embed["title"]
    assert "theta exit" in embed["description"]  # underscore → space


def test_build_trade_close_embed_negative_pnl_is_red() -> None:
    payload = discord.build_trade_close_embed(
        account_kind="aggressive",
        underlying="AMD",
        occ_symbol="AMD260117P00100000",
        close_reason="expired",
        total_cost_usd="100.00",
        realized_pnl_usd="-100.00",
    )
    embed = payload["embeds"][0]
    assert embed["color"] == 0xFF4D6D
    assert "-$100.00" in embed["title"]


def test_build_trade_close_embed_handles_unparseable_pnl() -> None:
    # Should not raise — defaults to 0/green.
    payload = discord.build_trade_close_embed(
        account_kind="conservative",
        underlying="X",
        occ_symbol="XYZ",
        close_reason="manual",
        total_cost_usd="0",
        realized_pnl_usd="not-a-number",
    )
    embed = payload["embeds"][0]
    assert embed["color"] == 0x2BD576  # 0 counts as non-negative


# === send(): delivery & resilience ===


@respx.mock
async def test_send_returns_true_on_2xx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", _WEBHOOK)
    get_settings.cache_clear()
    respx.post(_WEBHOOK).mock(return_value=httpx.Response(204))
    ok = await discord.send({"content": "hi"})
    assert ok is True


@respx.mock
async def test_send_returns_false_on_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", _WEBHOOK)
    get_settings.cache_clear()
    respx.post(_WEBHOOK).mock(return_value=httpx.Response(429, text="rate limited"))
    ok = await discord.send({"content": "hi"})
    assert ok is False


@respx.mock
async def test_send_returns_false_on_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", _WEBHOOK)
    get_settings.cache_clear()
    respx.post(_WEBHOOK).mock(return_value=httpx.Response(503, text="unavailable"))
    ok = await discord.send({"content": "hi"})
    assert ok is False


@respx.mock
async def test_send_returns_false_on_connect_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ConnectError (DNS / TCP failure) must NOT raise. Resilience contract."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", _WEBHOOK)
    get_settings.cache_clear()
    respx.post(_WEBHOOK).mock(side_effect=httpx.ConnectError("boom"))
    ok = await discord.send({"content": "hi"})
    assert ok is False


async def test_send_returns_false_when_webhook_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Same isolation trick as test_enabled_false_when_webhook_unset.
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "")
    get_settings.cache_clear()
    ok = await discord.send({"content": "hi"})
    assert ok is False


# === fire_and_forget(): the funnel-resilience guarantee ===


async def test_fire_and_forget_is_a_noop_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    get_settings.cache_clear()
    # Must not raise even though there's a running loop. Returns None either way.
    discord.fire_and_forget({"content": "ignored"})


@respx.mock
async def test_fire_and_forget_does_not_raise_on_webhook_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of fire_and_forget: a 503 webhook cannot kill the funnel."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", _WEBHOOK)
    get_settings.cache_clear()
    respx.post(_WEBHOOK).mock(return_value=httpx.Response(503))

    # Schedule, then yield so the background task runs to completion.
    discord.fire_and_forget({"content": "hi"})
    # Let the loop schedule + complete the task; one yield is enough since
    # the mocked POST returns immediately.
    await asyncio.sleep(0.05)
    # If the task had raised an unretrieved exception, this would show up
    # as a warning, not a test failure. We assert the simpler invariant:
    # we reached this line without an exception propagating from the scheduler.


def test_fire_and_forget_outside_running_loop_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Called from a sync context with no loop: drop quietly, don't crash."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", _WEBHOOK)
    get_settings.cache_clear()
    # No running loop here (sync test) — must not raise.
    discord.fire_and_forget({"content": "ignored"})


# === _truncate ===


def test_truncate_below_limit_is_unchanged() -> None:
    assert discord._truncate("short", 100) == "short"


def test_truncate_above_limit_ends_with_ellipsis() -> None:
    out = discord._truncate("x" * 100, 20)
    assert len(out) == 20
    assert out.endswith("…")


# === Embed schema sanity: every field renders to strings/ints Discord accepts ===


@pytest.mark.parametrize(
    "payload",
    [
        discord.build_thesis_embed(
            symbol="X",
            direction="long",
            confidence=0.5,
            source_bucket="manual",
            reasoning="ok",
            grounding_passed=True,
            occ_symbol="X",
            max_risk_usd="1.00",
        ),
        discord.build_trade_open_embed(
            account_kind="conservative",
            underlying="X",
            occ_symbol="X",
            option_type="call",
            contracts=1,
            total_cost_usd="1.00",
            risk_reason="ok",
        ),
        discord.build_trade_close_embed(
            account_kind="aggressive",
            underlying="X",
            occ_symbol="X",
            close_reason="expired",
            total_cost_usd="1.00",
            realized_pnl_usd="0.00",
        ),
    ],
)
def test_embed_payload_has_required_keys(payload: dict[str, Any]) -> None:
    assert "embeds" in payload
    embed = payload["embeds"][0]
    for key in ("title", "description", "color", "fields", "footer", "timestamp"):
        assert key in embed
    assert isinstance(embed["color"], int)
    assert embed["footer"]["text"].startswith("Terminal")
