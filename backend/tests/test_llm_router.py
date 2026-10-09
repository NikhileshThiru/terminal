"""LLM quota resilience: quota parsing, cooldown policy, router failover,
usage logging, and the provider-side 429 classification.

The scenario these guard against is the July 2026 incident: both free-tier
daily quotas exhausted mid-session, and every later event burned retries on
a dead quota and then vanished without a trace.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.data.types import ProviderUnavailable, ProviderUnavailableReason
from app.eval.models import Base
from app.llm.cost import get_cost_tracker
from app.llm.gemini import GeminiProvider
from app.llm.groq import GroqProvider
from app.llm.health import LLMHealthRegistry
from app.llm.interface import AgentTurn, LLMMessage, LLMProvider, LLMResponse, LLMUsage
from app.llm.quota import (
    is_daily_quota,
    parse_retry_after,
    seconds_until_pacific_midnight,
)
from app.llm.router import LLMRouter, Route
from app.llm.usage_log import DbUsageSink, LLMCall, UsageRecord, pacific_day_start, usage_since

# === quota parsing ===

GEMINI_DAILY = (
    "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your current "
    "quota', 'details': [{'@type': 'type.googleapis.com/google.rpc.QuotaFailure', "
    "'violations': [{'quotaId': 'GenerateRequestsPerDayPerProjectPerModel-FreeTier'}]}, "
    "{'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '41s'}]}}"
)
GEMINI_MINUTE = (
    "429 RESOURCE_EXHAUSTED. {'violations': [{'quotaId': "
    "'GenerateRequestsPerMinutePerProjectPerModel-FreeTier'}], 'retryDelay': '7s'}"
)
GROQ_TPD = (
    "Rate limit reached for model `openai/gpt-oss-20b` in organization `org_x` service "
    "tier `on_demand` on tokens per day (TPD): Limit 200000, Used 199757, Requested 1157. "
    "Please try again in 15m29.952s."
)


def test_parse_retry_after_dialects() -> None:
    assert parse_retry_after(GEMINI_DAILY) == 41.0
    assert parse_retry_after(GEMINI_MINUTE) == 7.0
    assert parse_retry_after(GROQ_TPD) == pytest.approx(15 * 60 + 29.952)
    assert parse_retry_after("Please retry in 1h2m3s") == 3723.0
    assert parse_retry_after("no hint here") is None


def test_daily_quota_detection() -> None:
    assert is_daily_quota(GEMINI_DAILY)
    assert is_daily_quota(GROQ_TPD)
    assert not is_daily_quota(GEMINI_MINUTE)


def test_seconds_until_pacific_midnight() -> None:
    # 2026-10-09 23:00 PDT == 2026-10-10 06:00 UTC -> 1 hour to midnight.
    now = datetime(2026, 10, 10, 6, 0, tzinfo=UTC)
    assert seconds_until_pacific_midnight(now) == 3600.0


# === health / cooldown policy ===


def _rl(hint: float | None = None) -> ProviderUnavailable:
    return ProviderUnavailable(
        reason=ProviderUnavailableReason.RATE_LIMITED,
        message="429",
        provider="p",
        retry_after_seconds=hint,
    )


def test_cooldown_uses_upstream_hint() -> None:
    reg = LLMHealthRegistry()
    assert reg.record_failure("gemini", "m", _rl(hint=120.0)) == 120.0
    assert reg.get("gemini", "m").cooling()


def test_cooldown_doubles_without_hint_and_caps() -> None:
    reg = LLMHealthRegistry(default_cooldown_seconds=60.0, max_cooldown_seconds=200.0)
    assert reg.record_failure("groq", "m", _rl()) == 60.0
    assert reg.record_failure("groq", "m", _rl()) == 120.0
    assert reg.record_failure("groq", "m", _rl()) == 200.0  # capped


def test_success_clears_cooldown_and_streak() -> None:
    reg = LLMHealthRegistry()
    reg.record_failure("groq", "m", _rl())
    reg.record_success("groq", "m")
    h = reg.get("groq", "m")
    assert not h.cooling()
    assert h.consecutive_rate_limits == 0
    assert reg.record_failure("groq", "m", _rl()) == 60.0  # streak restarted


def test_retired_model_is_parked_generic_error_is_not() -> None:
    reg = LLMHealthRegistry()
    gone = ProviderUnavailable(
        reason=ProviderUnavailableReason.MODEL_NOT_FOUND, message="404", retryable=False
    )
    assert reg.record_failure("groq", "old", gone) == 3600.0
    assert reg.record_failure("groq", "flaky", RuntimeError("boom")) is None
    assert not reg.get("groq", "flaky").cooling()


# === router ===


class _Answer(BaseModel):
    ok: bool


class _FakeLLM:
    """Scripted provider: each call pops the next outcome (value or exception)."""

    def __init__(self, name: str, outcomes: list[Any]) -> None:
        self.name = name
        self._outcomes = list(outcomes)
        self.models_called: list[str] = []

    def _next(self, model: str) -> Any:
        self.models_called.append(model)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        get_cost_tracker().record(provider=self.name, model=model, input_tokens=11, output_tokens=3)
        return outcome

    async def complete(self, messages: Any, model: str, **_: Any) -> LLMResponse:
        return LLMResponse(text=self._next(model), model=model, usage=LLMUsage())

    async def complete_structured(self, messages: Any, model: str, schema: Any, **_: Any) -> Any:
        return self._next(model)

    async def step_agent(self, conversation: Any, tools: Any, model: str, **_: Any) -> AgentTurn:
        return AgentTurn(text=self._next(model), usage=LLMUsage())


def _router(*routes: Route, sink: list[UsageRecord] | None = None) -> LLMRouter:
    return LLMRouter(
        step="triage",
        routes=list(routes),
        health=LLMHealthRegistry(),
        usage_sink=(sink.append if sink is not None else None),
    )


def _msgs() -> list[LLMMessage]:
    return [LLMMessage(role="user", content="hi")]


def test_router_satisfies_interface() -> None:
    assert isinstance(_router(Route(_FakeLLM("a", []), "m")), LLMProvider)


@pytest.mark.asyncio
async def test_router_uses_primary_when_healthy() -> None:
    a, b = _FakeLLM("a", [_Answer(ok=True)]), _FakeLLM("b", [])
    r = _router(Route(a, "a-1"), Route(b, "b-1"))
    out = await r.complete_structured(_msgs(), model="ignored", schema=_Answer)
    assert out.ok
    assert a.models_called == ["a-1"]  # route's own model, not the caller's
    assert b.models_called == []


@pytest.mark.asyncio
async def test_router_fails_over_on_rate_limit_and_parks_route() -> None:
    a = _FakeLLM("a", [_rl(hint=300.0)])
    b = _FakeLLM("b", [_Answer(ok=True), _Answer(ok=True)])
    r = _router(Route(a, "a-1"), Route(b, "b-1"))

    assert (await r.complete_structured(_msgs(), model="x", schema=_Answer)).ok
    # Second call: primary is cooling, so it is not even attempted.
    assert (await r.complete_structured(_msgs(), model="x", schema=_Answer)).ok
    assert a.models_called == ["a-1"]
    assert b.models_called == ["b-1", "b-1"]
    assert r.describe()[0]["state"] == "cooling"


@pytest.mark.asyncio
async def test_router_fails_over_on_generic_error_without_parking() -> None:
    a = _FakeLLM("a", [RuntimeError("500"), _Answer(ok=True)])
    b = _FakeLLM("b", [_Answer(ok=True)])
    r = _router(Route(a, "a-1"), Route(b, "b-1"))
    await r.complete_structured(_msgs(), model="x", schema=_Answer)
    await r.complete_structured(_msgs(), model="x", schema=_Answer)
    assert a.models_called == ["a-1", "a-1"]  # retried next call: no cooldown


@pytest.mark.asyncio
async def test_router_all_routes_rate_limited_raises_rate_limited_with_hint() -> None:
    a = _FakeLLM("a", [_rl(hint=60.0)])
    b = _FakeLLM("b", [_rl(hint=600.0)])
    r = _router(Route(a, "a-1"), Route(b, "b-1"))
    with pytest.raises(ProviderUnavailable) as exc:
        await r.complete_structured(_msgs(), model="x", schema=_Answer)
    assert exc.value.reason == ProviderUnavailableReason.RATE_LIMITED

    # Now both are cooling: fails fast without calling anyone, reports the
    # earliest recovery.
    with pytest.raises(ProviderUnavailable) as exc2:
        await r.complete_structured(_msgs(), model="x", schema=_Answer)
    assert exc2.value.reason == ProviderUnavailableReason.RATE_LIMITED
    assert exc2.value.retry_after_seconds is not None
    assert 0 < exc2.value.retry_after_seconds <= 60.0
    assert a.models_called == ["a-1"] and b.models_called == ["b-1"]


@pytest.mark.asyncio
async def test_router_mixed_failures_raise_upstream_error() -> None:
    a = _FakeLLM("a", [_rl()])
    b = _FakeLLM("b", [RuntimeError("bad json")])
    r = _router(Route(a, "a-1"), Route(b, "b-1"))
    with pytest.raises(ProviderUnavailable) as exc:
        await r.complete_structured(_msgs(), model="x", schema=_Answer)
    assert exc.value.reason == ProviderUnavailableReason.UPSTREAM_ERROR


@pytest.mark.asyncio
async def test_router_logs_every_attempt_with_tokens() -> None:
    sink: list[UsageRecord] = []
    a = _FakeLLM("a", [_rl(hint=30.0)])
    b = _FakeLLM("b", [_Answer(ok=True)])
    r = _router(Route(a, "a-1"), Route(b, "b-1"), sink=sink)
    await r.complete_structured(_msgs(), model="x", schema=_Answer)
    assert [(u.provider, u.ok, u.error_reason) for u in sink] == [
        ("a", False, "rate_limited"),
        ("b", True, None),
    ]
    assert (sink[1].input_tokens, sink[1].output_tokens) == (11, 3)
    assert sink[1].method == "complete_structured" and sink[1].step == "triage"


@pytest.mark.asyncio
async def test_router_stamps_agent_turn_provenance() -> None:
    a = _FakeLLM("a", [_rl()])
    b = _FakeLLM("b", ["thinking..."])
    r = _router(Route(a, "a-1"), Route(b, "b-1"))
    turn = await r.step_agent([], [], model="x")
    assert (turn.provider, turn.model) == ("b", "b-1")


# === usage log persistence ===


@pytest.mark.asyncio
async def test_db_usage_sink_writes_and_aggregates() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    sink = DbUsageSink(factory)
    sink(UsageRecord("triage", "groq", "g", "complete_structured", True, 120, 500, 40))
    sink(
        UsageRecord(
            "triage", "groq", "g", "complete_structured", False, 80, error_reason="rate_limited"
        )
    )
    sink(UsageRecord("thesis", "gemini", "f", "step_agent", True, 900, 4000, 300))
    # Yesterday's row must not count toward "today".
    sink(
        UsageRecord(
            "thesis",
            "gemini",
            "f",
            "step_agent",
            True,
            1,
            called_at=pacific_day_start() - timedelta(hours=1),
        )
    )
    await sink.drain()

    async with factory() as session:
        rows = await usage_since(session, pacific_day_start())
        assert (await session.get(LLMCall, 1)) is not None
    by_key = {(r["step"], r["provider"]): r for r in rows}
    assert by_key[("triage", "groq")]["calls"] == 2
    assert by_key[("triage", "groq")]["ok"] == 1
    assert by_key[("triage", "groq")]["rate_limited"] == 1
    assert by_key[("triage", "groq")]["input_tokens"] == 500
    assert by_key[("thesis", "gemini")]["calls"] == 1
    await engine.dispose()


# === provider-side 429 classification ===


@pytest.mark.asyncio
@respx.mock
async def test_groq_daily_quota_raises_immediately_with_hint() -> None:
    route = respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(429, json={"error": {"message": GROQ_TPD}})
    )
    p = GroqProvider(api_key="k", rate_per_minute=10000, max_retries=3)
    with pytest.raises(ProviderUnavailable) as exc:
        await p.complete(messages=_msgs(), model="openai/gpt-oss-20b")
    assert exc.value.reason == ProviderUnavailableReason.RATE_LIMITED
    assert exc.value.retry_after_seconds == pytest.approx(929.952)
    assert route.call_count == 1  # no retries burned on a daily cap
    await p.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_groq_short_429_is_retried_inline() -> None:
    route = respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "0"}, json={"error": {"message": "TPM"}}),
            httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}], "usage": {}}),
        ]
    )
    p = GroqProvider(api_key="k", rate_per_minute=10000, max_retries=2)
    r = await p.complete(messages=_msgs(), model="openai/gpt-oss-20b")
    assert r.text == "hi"
    assert route.call_count == 2
    await p.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_groq_retired_model_is_model_not_found() -> None:
    respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(404, json={"error": {"message": "model not found"}})
    )
    p = GroqProvider(api_key="k", rate_per_minute=10000, max_retries=0)
    with pytest.raises(ProviderUnavailable) as exc:
        await p.complete(messages=_msgs(), model="llama-3.3-70b-versatile")
    assert exc.value.reason == ProviderUnavailableReason.MODEL_NOT_FOUND
    assert exc.value.retryable is False
    await p.aclose()


def _gemini_raising(message: str) -> tuple[GeminiProvider, list[int]]:
    calls: list[int] = []

    class _Models:
        async def generate_content(self, **_: Any) -> Any:
            calls.append(1)
            raise RuntimeError(message)

    p = GeminiProvider(api_key="k", rate_per_minute=10000, max_retries=3, initial_backoff_seconds=0)
    p._client = SimpleNamespace(aio=SimpleNamespace(models=_Models()))  # type: ignore[assignment]
    return p, calls


@pytest.mark.asyncio
async def test_gemini_daily_quota_waits_for_pacific_midnight_not_retries() -> None:
    p, calls = _gemini_raising(GEMINI_DAILY)
    with pytest.raises(ProviderUnavailable) as exc:
        await p.complete(messages=_msgs(), model="gemini-2.5-flash")
    assert exc.value.reason == ProviderUnavailableReason.RATE_LIMITED
    assert calls == [1]
    assert exc.value.retry_after_seconds is not None
    assert exc.value.retry_after_seconds >= 41.0


@pytest.mark.asyncio
async def test_gemini_long_minute_hint_fails_over_instead_of_sleeping() -> None:
    p, calls = _gemini_raising(GEMINI_MINUTE.replace("'7s'", "'45s'"))
    with pytest.raises(ProviderUnavailable) as exc:
        await p.complete(messages=_msgs(), model="gemini-2.5-flash")
    assert exc.value.reason == ProviderUnavailableReason.RATE_LIMITED
    assert exc.value.retry_after_seconds == 45.0
    assert calls == [1]  # 45s > 30s inline ceiling: hand back to the router


@pytest.mark.asyncio
async def test_gemini_missing_model_is_model_not_found() -> None:
    p, _ = _gemini_raising("404 NOT_FOUND. models/gemini-0 is not found")
    with pytest.raises(ProviderUnavailable) as exc:
        await p.complete(messages=_msgs(), model="gemini-0")
    assert exc.value.reason == ProviderUnavailableReason.MODEL_NOT_FOUND


# === factory config parsing ===


def test_route_specs_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings
    from app.llm.factory import parse_route_spec, route_specs

    monkeypatch.setenv("LLM_TRIAGE_PROVIDER", "groq")
    monkeypatch.setenv("LLM_TRIAGE_MODEL", "openai/gpt-oss-20b")
    monkeypatch.setenv(
        "LLM_TRIAGE_FALLBACKS", "gemini:gemini-2.5-flash-lite, groq:openai/gpt-oss-20b"
    )
    get_settings.cache_clear()
    # Duplicate of the primary is dropped; model case is preserved.
    assert route_specs("triage") == [
        ("groq", "openai/gpt-oss-20b"),
        ("gemini", "gemini-2.5-flash-lite"),
    ]
    with pytest.raises(ValueError):
        parse_route_spec("no-colon")


# === Gemini 3 thought signatures ===


def test_gemini_signature_echoed_to_issuing_model_only() -> None:
    """Gemini 3 400s on a function-call turn without its thought_signature.
    The signature goes back only to the model that issued it; a different
    Gemini 3 model (router failover mid-thesis) gets the documented skip
    sentinel; Gemini 2.5 gets nothing."""
    import base64

    from app.llm.gemini import _SKIP_SIGNATURE, _agent_messages_to_gemini
    from app.llm.interface import AgentMessage, AgentToolCall

    sig = base64.b64encode(b"opaque-sig").decode()
    call = AgentToolCall(
        id="c1",
        name="get_quote",
        arguments={"symbol": "AAPL"},
        signature=sig,
        signature_model="gemini-3.5-flash",
    )
    conv = [AgentMessage(role="assistant", tool_calls=[call])]

    def part_sig(target: str) -> bytes | None:
        _, contents = _agent_messages_to_gemini(conv, target)
        return contents[0].parts[0].thought_signature  # type: ignore[index]

    assert part_sig("gemini-3.5-flash") == b"opaque-sig"
    assert part_sig("gemini-3.8-flash") == _SKIP_SIGNATURE
    assert part_sig("gemini-2.5-flash") is None


@pytest.mark.asyncio
async def test_gemini_overload_retries_once_then_hands_off_to_router() -> None:
    """503 "high demand": one quick retry, then UPSTREAM_ERROR so the router
    fails over — not 56 s of exponential backoff on an overloaded model."""
    calls: list[int] = []

    class _Overloaded:
        async def generate_content(self, **_: Any) -> Any:
            calls.append(1)
            raise RuntimeError("503 UNAVAILABLE. This model is currently experiencing high demand.")

    p = GeminiProvider(api_key="k", rate_per_minute=10000, transient_backoff_seconds=0)
    p._client = SimpleNamespace(aio=SimpleNamespace(models=_Overloaded()))  # type: ignore[assignment]
    with pytest.raises(ProviderUnavailable) as exc:
        await p.complete(messages=_msgs(), model="gemini-2.5-flash")
    assert exc.value.reason == ProviderUnavailableReason.UPSTREAM_ERROR
    assert len(calls) == 2  # original + exactly one retry
