"""Tests for GET /health/active (Phase 2 W3c — LB target endpoint)."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def app_factory(ephemeral_ca):
    from tests.conftest import make_app_with_state
    def _make(controller_state: str):
        return make_app_with_state(
            ephemeral_ca, enrollment_token="ignored", controller_state=controller_state,
        )
    return _make


@pytest.mark.slow
async def test_health_active_503_when_standby(app_factory) -> None:
    app = app_factory("standby")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/health/active")
    assert r.status_code == 503
    assert r.json()["detail"]["controller_state"] == "standby"


@pytest.mark.slow
async def test_health_active_200_when_recovering(app_factory) -> None:
    app = app_factory("recovering")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/health/active")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "active"
    assert body["controller_state"] == "recovering"


@pytest.mark.slow
async def test_health_active_200_when_active(app_factory) -> None:
    app = app_factory("active")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/health/active")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "active"
    assert body["controller_state"] == "active"
