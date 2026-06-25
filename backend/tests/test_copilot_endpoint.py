"""POST /copilot/thesis — integration test with stubbed copilot."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent.copilot import CopilotRun
from app.agent.grounding import GroundingResult
from app.agent.schemas import SuggestedContract, Thesis
from app.api.copilot import get_copilot
from app.eval import persistence
from app.eval.models import Base
from app.main import create_app


def _sample_thesis() -> Thesis:
    return Thesis(
        symbol="AAPL",
        direction="long",
        confidence=0.65,
        reasoning="AAPL at $312.06; building a long via the 315 call.",
        prediction_window_days=14,
        suggested_contract=SuggestedContract(
            underlying="AAPL",
            occ_symbol="AAPL270115C00315000",
            option_type="call",
            strike=Decimal("315"),
            expiration=date(2027, 1, 15),
            estimated_premium_per_contract=Decimal("4.60"),
            contracts=1,
            max_risk_usd=Decimal("460"),
        ),
        what_must_happen="AAPL closes above $315 by expiry.",
        correlation_id="test-corr-id",
        source_bucket="manual",
        generated_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        grounding_check_passed=True,
        llm_provider="gemini",
        llm_model="gemini-2.5-flash",
        funnel_latency_ms=4321,
    )


class _StubCopilot:
    async def generate(
        self,
        user_thesis: str,
        *,
        risk_budget_usd: float | None = None,
        source_bucket: str = "manual",
    ) -> CopilotRun:
        return CopilotRun(
            thesis=_sample_thesis(),
            tool_results=[],
            grounding=GroundingResult(
                passed=True,
                unverified_numbers=[],
                tools_used=[],
                extracted_count=0,
            ),
            iterations_used=2,
        )


@pytest.fixture(autouse=True)
async def _isolated_db(monkeypatch: pytest.MonkeyPatch):
    """Each test gets a clean in-memory SQLite, schema applied via metadata.

    We swap persistence's cached engine/session factory for one bound to this DB.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    # Clear lru_cache BEFORE monkeypatching so any cached values are dropped.
    persistence.get_engine.cache_clear()
    persistence.get_session_factory.cache_clear()
    monkeypatch.setattr(persistence, "get_engine", lambda: engine)
    monkeypatch.setattr(persistence, "get_session_factory", lambda: factory)
    yield
    await engine.dispose()


def _client_with_stub() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_copilot] = lambda: _StubCopilot()  # type: ignore[return-value]
    return TestClient(app)


def test_post_thesis_returns_structured_thesis() -> None:
    client = _client_with_stub()
    r = client.post(
        "/copilot/thesis",
        json={"user_thesis": "AAPL looks strong into earnings", "risk_budget_usd": 500},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["symbol"] == "AAPL"
    assert body["direction"] == "long"
    assert body["confidence"] == 0.65
    assert body["suggested_contract"]["occ_symbol"] == "AAPL270115C00315000"
    assert body["grounding_check_passed"] is True
    assert body["llm_provider"] == "gemini"


def test_post_thesis_rejects_empty_input() -> None:
    client = _client_with_stub()
    r = client.post("/copilot/thesis", json={"user_thesis": "hi"})
    assert r.status_code == 422


def test_post_thesis_rejects_negative_budget() -> None:
    client = _client_with_stub()
    r = client.post(
        "/copilot/thesis",
        json={"user_thesis": "AAPL strong setup ahead of earnings", "risk_budget_usd": -1},
    )
    assert r.status_code == 422


def test_post_thesis_returns_correlation_id_header() -> None:
    client = _client_with_stub()
    r = client.post(
        "/copilot/thesis",
        json={"user_thesis": "AAPL strong setup ahead of earnings"},
    )
    assert r.status_code == 200
    assert "x-correlation-id" in {k.lower() for k in r.headers}
