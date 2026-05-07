"""Bearer token middleware tests using FastAPI ASGI transport."""
from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from dlw.auth.bearer import require_bearer
from dlw.config import get_settings


@pytest.fixture(autouse=True)
def _reset_settings():
    """Each test gets fresh settings cache so DLW_BEARER_TOKEN env can vary."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _app_with_protected_route() -> FastAPI:
    app = FastAPI()
    @app.get("/protected", dependencies=[Depends(require_bearer)])
    async def _():
        return {"ok": True}
    return app


@pytest.mark.slow
async def test_no_token_returns_401() -> None:
    app = _app_with_protected_route()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/protected")
    assert r.status_code == 401
    assert r.json()["detail"] == "missing bearer token"


@pytest.mark.slow
async def test_wrong_token_returns_401() -> None:
    app = _app_with_protected_route()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/protected", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid bearer token"


@pytest.mark.slow
async def test_correct_token_returns_200(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DLW_BEARER_TOKEN", "secret-xyz")
    get_settings.cache_clear()
    app = _app_with_protected_route()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/protected", headers={"Authorization": "Bearer secret-xyz"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


@pytest.mark.slow
async def test_malformed_authorization_returns_401() -> None:
    app = _app_with_protected_route()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/protected", headers={"Authorization": "secret-xyz"})
    assert r.status_code == 401
