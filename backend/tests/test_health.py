"""Verify the health endpoint and correlation-id middleware."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app


def test_health_returns_ok() -> None:
    client = TestClient(create_app())
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert "version" in data
    assert "timestamp" in data


def test_correlation_id_generated_when_absent() -> None:
    client = TestClient(create_app())
    r = client.get("/health")
    header_keys = {k.lower() for k in r.headers}
    assert "x-correlation-id" in header_keys
    assert len(r.headers["x-correlation-id"]) == 16


def test_correlation_id_honored_when_provided() -> None:
    client = TestClient(create_app())
    r = client.get("/health", headers={"X-Correlation-ID": "abc-test-id"})
    assert r.headers["x-correlation-id"] == "abc-test-id"
