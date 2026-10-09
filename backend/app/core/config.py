"""Application configuration (DESIGN.md §2.7, §7: config-driven).

Loaded from environment + .env file. Never hardcode model choice, confidence
thresholds, ticker universe, or rate limits — they all live here.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# .env lives at the project root (so docker-compose can mount it across services).
# Resolve from this file's path so the app finds it regardless of CWD.
# backend/app/core/config.py → parents[3] is the project root.
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_ENV_FILE = _PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # === Environment ===
    environment: Literal["development", "production", "test"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # === Infrastructure ===
    database_url: str = "postgresql+psycopg://terminal:terminal@localhost:5432/terminal"
    redis_url: str = "redis://localhost:6379/0"

    # === LLM providers (Phase 4) ===
    # Cloud: Gemini + Groq (both free-tier). There is NO Anthropic provider by
    # design: the Anthropic API has no free tier (would break the free rule)
    # and the owner's Claude subscription must never be consumed by this
    # project. Local Ollama was considered and cut (multi-GB model download
    # for a last-resort path the two-cloud fallback already covers).
    gemini_api_key: str | None = None
    groq_api_key: str | None = None
    # Primary route per step. Groq's free models are fast and have their own
    # per-model daily quotas, which keeps triage off Gemini's quota entirely.
    llm_triage_provider: Literal["gemini", "groq"] = "groq"
    llm_triage_model: str = "openai/gpt-oss-20b"
    llm_thesis_provider: Literal["gemini", "groq"] = "gemini"
    llm_thesis_model: str = "gemini-2.5-flash"
    # Ordered fallbacks ("provider:model", comma-separated in env). Free-tier
    # quotas are PER MODEL, and Gemini's are small (~20 requests/day/model on
    # the 2026 free tier, observed), so each extra model is real capacity:
    # - thesis: seven Gemini models ≈ 7x the daily theses of one (the first
    #   live morning exhausted three of the original four by 09:40). Gemini 3
    #   needs thought signatures echoed across tool turns (handled in
    #   app/llm/gemini.py, including mid-thesis failover between models).
    #   Groq is absent: its 8K tokens/minute cap is smaller than one
    #   multi-tool thesis request.
    # - triage: Groq first (1,000 req/day per model), then a Gemini model the
    #   thesis chain does NOT use, so triage can never starve theses.
    llm_triage_fallbacks: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["groq:openai/gpt-oss-120b", "gemini:gemini-3.1-flash-lite"]
    )
    llm_thesis_fallbacks: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "gemini:gemini-3.5-flash",
            "gemini:gemini-3.7-flash",
            "gemini:gemini-3.6-flash",
            "gemini:gemini-3-flash-preview",
            "gemini:gemini-3.8-flash",  # slowest under load (~90 s/call observed)
            "gemini:gemini-2.5-flash-lite",  # weakest: last resort
        ]
    )
    # Cooldown after a 429 with no upstream retry hint; doubles per
    # consecutive 429 up to the max. Daily-quota 429s use the upstream hint
    # (or midnight Pacific for Gemini) instead.
    llm_default_cooldown_seconds: float = 60.0
    llm_max_cooldown_seconds: float = 1800.0
    # Free-tier request budget per day, rendered as the header's
    # calls-vs-budget chip (counted from the llm_calls table, Pacific day).
    # Sum of the routes' daily caps is higher; this is the comfort line.
    llm_daily_request_budget: int = 1500

    # === Market data (Phase 2) ===
    alpaca_api_key: str | None = None
    alpaca_api_secret: str | None = None
    alpaca_base_url: str = "https://paper-api.alpaca.markets"
    finnhub_api_key: str | None = None
    fred_api_key: str | None = None
    sec_user_agent: str = "Terminal Research/0.1 (set-SEC_USER_AGENT@example.com)"

    # === App config ===
    # NoDecode keeps pydantic-settings from trying to JSON-parse the env var first.
    # Our validator splits comma-separated strings into a list.
    watchlist: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            # Mega-cap tech
            "AAPL",
            "MSFT",
            "NVDA",
            "AMD",
            "GOOGL",
            "META",
            "AMZN",
            "TSLA",
            "AVGO",
            "ORCL",
            "CRM",
            "NFLX",
            # Finance
            "JPM",
            "BAC",
            "GS",
            "V",
            # Healthcare
            "LLY",
            "UNH",
            "JNJ",
            # Consumer
            "WMT",
            "COST",
            "MCD",
            # Industrial / energy
            "BA",
            "XOM",
            # Broad-market ETFs (sticky context for indices)
            "SPY",
            "QQQ",
            "IWM",
        ]
    )

    # === Funnel performance budgets (DESIGN.md §4) ===
    funnel_catalyst_budget_seconds: float = 60.0
    funnel_reactive_budget_seconds: float = 300.0

    # === Triage / thesis thresholds (Phase 4) ===
    triage_pass_min_score: float = 0.6
    thesis_min_confidence: float = 0.55

    # === Paper-account risk config (Phase 7, DESIGN.md §8) ===
    # Conservative: higher threshold, smaller positions, fewer trades.
    conservative_account_min_confidence: float = 0.70
    conservative_account_max_trade_cost_usd: float = 300.0
    conservative_account_max_trades_per_day: int = 3
    conservative_account_max_concurrent_positions: int = 5
    # Aggressive: lower threshold, bigger positions, more trades.
    aggressive_account_min_confidence: float = 0.50
    aggressive_account_max_trade_cost_usd: float = 500.0
    aggressive_account_max_trades_per_day: int = 8
    aggressive_account_max_concurrent_positions: int = 10

    # === Autonomous discovery (Phase 6) ===
    autonomous_risk_budget_usd: float = 500.0
    # Thesis pacing: each reactive thesis costs ~9 thesis-model calls and the
    # whole free Gemini chain allows roughly 7 theses/day; on the first live
    # morning reactive news alone exhausted 5 of 7 models by 10:00 ET. A small
    # cap keeps quota for the (higher-value) catalyst path. Events that pass triage
    # after the cap is hit are recorded as skipped, visibly. Catalyst theses
    # don't count against it.
    autonomous_max_reactive_theses_per_day: int = 4
    discovery_poll_interval_seconds: float = 300.0
    # Gate autonomous thesis generation (reactive + catalyst) to the regular
    # US equity session (CLAUDE.md §7). Off-session events are dropped with a
    # visible triage decision; catalysts simply wait for the next open-hours
    # tick. Set false in dev to test with synthetic events at any hour.
    market_hours_gate: bool = True
    # When true, the FastAPI lifespan starts the worker automatically — the
    # autonomous mode IS the product, so manual toggling shouldn't be a
    # prerequisite for the dashboard to feel alive.
    autonomous_autostart: bool = True

    # === Event bus + ingestion topology (CLAUDE.md §3-4) ===
    # memory  = asyncio.Queue inside the backend process (tests, plain uvicorn).
    # redis   = Redis Streams consumer group (the docker-compose stack).
    event_bus: Literal["memory", "redis"] = "memory"
    # embedded = the worker runs the EDGAR poller + Alpaca news WS itself.
    # external = a separate `ingestion` service publishes to the Redis bus and
    #            reports its health via a Redis key (requires event_bus=redis).
    ingestion_mode: Literal["embedded", "external"] = "embedded"
    event_stream_key: str = "terminal:events"
    # ~2 months of events at ~800/day: the stream doubles as the replay log.
    event_stream_maxlen: int = 50_000
    # Reactive staleness gate: an event that sat on the bus longer than this
    # (worker was down, backlog after restart) is skipped visibly instead of
    # spending quota on news the market priced in long ago (CLAUDE.md §4:
    # reactive target is minutes, not hours).
    reactive_max_event_age_seconds: float = 900.0

    # === Reconciliation jobs (Phase 7+, DESIGN.md §8) ===
    mtm_interval_seconds: float = 300.0  # mark-to-market every 5 min
    resolver_interval_seconds: float = 1800.0  # outcome resolution every 30 min

    # === Catalyst calendar (Step 5, DESIGN.md §8) ===
    # Lead window: how far in advance to pre-position on a known catalyst.
    catalyst_lead_days: int = 2
    # How often to refresh the calendar from Finnhub. The earnings calendar
    # only changes meaningfully day-to-day, so 6h is plenty.
    catalyst_fetcher_interval_seconds: float = 21600.0  # 6 hours
    # How often to look for ready-to-trigger catalysts. Within-day granularity
    # is fine since lead_days is the dominant timescale.
    catalyst_scheduler_interval_seconds: float = 3600.0  # 1 hour
    # Horizon for upcoming-events queries (how far ahead to fetch).
    catalyst_horizon_days: int = 60

    # === Rate limits (per-source) ===
    edgar_requests_per_second: float = 10.0
    finnhub_requests_per_minute: float = 60.0
    alpaca_requests_per_minute: float = 200.0

    # === Cache TTLs in seconds ===
    cache_ttl_fundamentals: int = 3600
    cache_ttl_filings_list: int = 600
    cache_ttl_quotes: int = 5

    # === Notifications (Step 9.1) ===
    # Discord webhook for thesis/trade alerts. Absent → feature off.
    discord_webhook_url: str | None = None

    @field_validator("watchlist", mode="before")
    @classmethod
    def _split_watchlist(cls, v: object) -> object:
        # Allow comma-separated strings from env vars
        if isinstance(v, str):
            return [s.strip().upper() for s in v.split(",") if s.strip()]
        return v

    @field_validator("llm_triage_fallbacks", "llm_thesis_fallbacks", mode="before")
    @classmethod
    def _split_routes(cls, v: object) -> object:
        # "gemini:gemini-2.5-flash-lite,groq:openai/gpt-oss-120b" -> list.
        # Model ids keep their case (Groq ids are case-sensitive).
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    @property
    def is_test(self) -> bool:
        return self.environment == "test"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor. Use this everywhere; do not call `Settings()` directly."""
    return Settings()
