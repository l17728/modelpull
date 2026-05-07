"""Health endpoint tests using httpx ASGI transport (no real network bind)."""
from __future__ import annotations

import os

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine


@pytest.mark.slow
async def test_health_live_returns_200() -> None:
    from dlw.main import create_app
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


@pytest.mark.slow
async def test_health_ready_returns_200_when_db_reachable(
    monkeypatch: pytest.MonkeyPatch,
    engine: AsyncEngine,
    test_db_name: str,
) -> None:
    """Ready check uses the per-session test DB (no dependency on a manually
    created `dlw` database). The `engine` fixture creates test_dlw_<random>
    via CREATE DATABASE; we point app config at it via env vars."""
    monkeypatch.setenv("DLW_DB_HOST", os.environ.get("DLW_TEST_PG_HOST", "localhost"))
    monkeypatch.setenv("DLW_DB_PORT", os.environ.get("DLW_TEST_PG_PORT", "5433"))
    monkeypatch.setenv("DLW_DB_USER", os.environ.get("DLW_TEST_PG_USER", "postgres"))
    monkeypatch.setenv("DLW_DB_PASSWORD", os.environ.get("DLW_TEST_PG_PASSWORD", ""))
    monkeypatch.setenv("DLW_DB_NAME", test_db_name)

    # Re-build settings + engine caches against the new env
    from dlw.config import get_settings
    from dlw.db.session import reset_engine
    get_settings.cache_clear()
    await reset_engine()

    from dlw.main import create_app
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["db"] == "ok"

    # Cleanup so subsequent tests get a fresh engine
    await reset_engine()
    get_settings.cache_clear()
