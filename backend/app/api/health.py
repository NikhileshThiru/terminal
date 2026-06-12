"""Health check endpoint — used by the frontend to verify backend connectivity."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter
from pydantic import BaseModel

from app import __version__

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    version: str
    timestamp: datetime


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness check. Returns 200 if the process is up."""
    return HealthResponse(
        status="ok",
        version=__version__,
        timestamp=datetime.now(UTC),
    )
