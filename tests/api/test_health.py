"""Health endpoint tests using httpx ASGI transport (no real network bind)."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.slow
async def test_health_live_returns_200() -> None:
    from dlw.main import create_app
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


@pytest.mark.slow
async def test_health_ready_returns_200_when_db_reachable() -> None:
    """Ready check requires DB; uses the local dlw database (Task 11 created it)."""
    # Make sure config points at the dlw database (defaults already do)
    import os
    os.environ.setdefault("DLW_DB_HOST", "localhost")
    os.environ.setdefault("DLW_DB_PORT", "5433")
    os.environ.setdefault("DLW_DB_USER", "postgres")
    os.environ.setdefault("DLW_DB_PASSWORD", "")
    os.environ.setdefault("DLW_DB_NAME", "dlw")

    # Reset settings cache so env vars are picked up
    from dlw.config import get_settings
    get_settings.cache_clear()

    from dlw.main import create_app
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["db"] == "ok"
