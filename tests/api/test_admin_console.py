"""v2.1 Sprint 13 — admin Live Console REST tests."""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from httpx import ASGITransport, AsyncClient

from dlw.services.reverse_dispatcher import get_dispatcher
from dlw.services.reverse_ws_registry import get_registry
from tests.conftest import make_app_with_state, principal_headers


SECRET = "unit-secret-console"


@dataclass
class _StubWS:
    sent: list[str] = field(default_factory=list)

    async def send_text(self, raw: str) -> None:
        self.sent.append(raw)

    async def close(self, code: int) -> None:
        pass


@pytest.fixture(autouse=True)
def _reset_singletons(monkeypatch):
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", SECRET)
    get_dispatcher()._reset_for_tests()
    get_registry()._reset_for_tests()
    from dlw.config import get_settings
    get_settings.cache_clear()
    yield
    get_dispatcher()._reset_for_tests()
    get_registry()._reset_for_tests()
    get_settings.cache_clear()


@pytest.fixture
async def client(ephemeral_ca):
    app = make_app_with_state(ephemeral_ca, enrollment_token="e")
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test", timeout=5.0) as c:
        yield c


def _admin_headers() -> dict[str, str]:
    return principal_headers(secret=SECRET, role="system_admin")


def _tenant_headers() -> dict[str, str]:
    return principal_headers(secret=SECRET, role="tenant_admin")


# ---------------------------------------------------------------------------
# POST /admin/executors/{id}/command

async def test_command_happy_path(client: AsyncClient):
    ws = _StubWS()
    await get_registry().register(
        executor_id="ex-1", websocket=ws, protocol_version="1.0")
    resp = await client.post(
        "/api/v1/admin/executors/ex-1/command",
        json={"command": "status"},
        headers=_admin_headers())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["executor_id"] == "ex-1"
    assert body["command"] == "status"
    assert body["command_id"]
    assert len(ws.sent) == 1


async def test_command_requires_system_admin(client: AsyncClient):
    """tenant_admin gets 403."""
    ws = _StubWS()
    await get_registry().register(
        executor_id="ex-2", websocket=ws, protocol_version="1.0")
    resp = await client.post(
        "/api/v1/admin/executors/ex-2/command",
        json={"command": "status"},
        headers=_tenant_headers())
    assert resp.status_code == 403


async def test_command_offline_executor_returns_404(client: AsyncClient):
    resp = await client.post(
        "/api/v1/admin/executors/never-connected/command",
        json={"command": "status"},
        headers=_admin_headers())
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "EXECUTOR_OFFLINE"


async def test_command_not_whitelisted_returns_422(client: AsyncClient):
    ws = _StubWS()
    await get_registry().register(
        executor_id="ex-3", websocket=ws, protocol_version="1.0")
    resp = await client.post(
        "/api/v1/admin/executors/ex-3/command",
        json={"command": "rm-rf-everything"},
        headers=_admin_headers())
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "NOT_WHITELISTED"
    assert "status" in detail["allowed"]
    # No frame was sent — whitelist rejection is before the WS send
    assert ws.sent == []


# ---------------------------------------------------------------------------
# GET /admin/reverse-ws/sessions

async def test_sessions_list_returns_all_live(client: AsyncClient):
    await get_registry().register(
        executor_id="ex-A", websocket=_StubWS(), protocol_version="1.0")
    await get_registry().register(
        executor_id="ex-B", websocket=_StubWS(), protocol_version="1.0")
    resp = await client.get("/api/v1/admin/reverse-ws/sessions",
                             headers=_admin_headers())
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 2
    ids = {s["executor_id"] for s in items}
    assert ids == {"ex-A", "ex-B"}
    for s in items:
        assert s["protocol_version"] == "1.0"
        assert s["session_id"]


async def test_sessions_list_empty_when_no_connections(client: AsyncClient):
    resp = await client.get("/api/v1/admin/reverse-ws/sessions",
                             headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json() == {"items": []}


async def test_sessions_list_requires_system_admin(client: AsyncClient):
    resp = await client.get("/api/v1/admin/reverse-ws/sessions",
                             headers=_tenant_headers())
    assert resp.status_code == 403
