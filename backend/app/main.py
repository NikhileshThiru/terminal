"""FastAPI application factory.

Use `create_app()` (composable, testable) rather than a module-level singleton
when wiring tests. The module-level `app` exists for `uvicorn app.main:app`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.agent import router as agent_router
from app.api.autonomous import router as autonomous_router
from app.api.bars import router as bars_router
from app.api.chain import router as chain_router
from app.api.copilot import router as copilot_router
from app.api.eval import router as eval_router
from app.api.health import router as health_router
from app.api.llm import router as llm_router
from app.api.portfolio import router as portfolio_router
from app.api.prices import router as prices_router
from app.api.tickers import router as tickers_router
from app.core.config import get_settings
from app.core.correlation import new_correlation_id, set_correlation_id
from app.core.logging import configure_logging, get_logger


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Per-request correlation ID. Honors incoming X-Correlation-ID."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        incoming = request.headers.get("x-correlation-id")
        cid = incoming if incoming else new_correlation_id()
        set_correlation_id(cid)
        response: Response = await call_next(request)
        response.headers["X-Correlation-ID"] = cid
        return response


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    log = get_logger("app.startup")
    settings = get_settings()
    log.info(
        "app_starting",
        environment=settings.environment,
        log_level=settings.log_level,
        watchlist_size=len(settings.watchlist),
    )
    # Seed the two default paper accounts (idempotent — no-op after first run).
    try:
        from app.eval.persistence import get_session_factory
        from app.portfolio.seed import seed_default_accounts

        created = await seed_default_accounts(get_session_factory())
        if created:
            log.info("paper_accounts_seeded_at_startup", created=created)
    except Exception:
        log.exception("paper_accounts_seed_failed")

    # Autostart the autonomous worker on boot when configured. The product's
    # default mode is "always watching"; a manual toggle shouldn't be a
    # prerequisite for the dashboard to feel alive.
    worker = None
    if settings.autonomous_autostart and settings.environment != "test":
        try:
            from app.discovery.worker import get_worker

            worker = get_worker()
            await worker.start()
            log.info("autonomous_worker_autostarted")
        except Exception:
            log.exception("autonomous_worker_autostart_failed")
    yield
    if worker is not None:
        try:
            await worker.stop()
            log.info("autonomous_worker_stopped_on_shutdown")
        except Exception:
            log.exception("autonomous_worker_shutdown_failed")
    log.info("app_stopping")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Terminal",
        description="AI-native trading research terminal (paper trading only).",
        version="0.1.0",
        lifespan=lifespan,
    )
    # Dev frontend runs on localhost; allow it to call the backend. We accept
    # ANY localhost port in development (not just :5173) so the frontend works
    # regardless of which host port it's published on — e.g. when the Docker
    # stack remaps 5173→5174 to dodge a conflict with another local project.
    # Production stays locked down (no origins allowed).
    is_dev = settings.environment == "development"
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?" if is_dev else None,
        allow_origins=[],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Correlation-ID"],
    )
    app.add_middleware(CorrelationIdMiddleware)
    app.include_router(health_router)
    app.include_router(copilot_router)
    app.include_router(autonomous_router)
    app.include_router(portfolio_router)
    app.include_router(eval_router)
    app.include_router(chain_router)
    app.include_router(bars_router)
    app.include_router(prices_router)
    app.include_router(llm_router)
    app.include_router(agent_router)
    app.include_router(tickers_router)
    return app


app = create_app()
