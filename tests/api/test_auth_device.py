"""RFC 8628 device-code endpoint tests (FU6 — Milestone M2)."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.config import get_settings
from dlw.db.base import Base

# ---------------------------------------------------------------------------
# Module-level DB bootstrap (mirrors test_auth_endpoints.py)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    from dlw.db.models.tenant import Project, Tenant
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(Tenant(id=1, slug="default", display_name="Default"))
        await s.flush()
        s.add(Project(id=1, tenant_id=1, name="default"))
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


# ---------------------------------------------------------------------------
# Autouse fixture — set dev-mode env + clear settings cache BEFORE client
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _dev_mode(monkeypatch):
    monkeypatch.setenv("DLW_AUTH_DEV_MODE", "true")
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", "unit-secret")
    monkeypatch.setenv(
        "DLW_AUTH_TENANT_RULES_JSON",
        '[{"match":"email_domain","value":"*","tenant_slug":"default",'
        '"role":"tenant_operator"}]')
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Normal client fixture (no service token)
# ---------------------------------------------------------------------------

@pytest.fixture
async def client(ephemeral_ca):
    from tests.conftest import make_app_with_state
    app = make_app_with_state(ephemeral_ca, enrollment_token="e")
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Service-principal client (sets DLW_SYSTEM_ADMIN_TOKEN BEFORE app builds)
# ---------------------------------------------------------------------------

@pytest.fixture
async def service_client(ephemeral_ca, monkeypatch):
    """App built AFTER setting the admin token env so require_principal sees it."""
    monkeypatch.setenv("DLW_SYSTEM_ADMIN_TOKEN", "svc-tok")
    get_settings.cache_clear()
    from tests.conftest import make_app_with_state
    app = make_app_with_state(ephemeral_ca, enrollment_token="e")
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _principal_headers(**kwargs):
    from tests.conftest import principal_headers
    return principal_headers(**kwargs)


def _service_headers(token: str = "svc-tok"):
    from tests.conftest import service_headers
    return service_headers(token)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.slow
async def test_device_happy_path(client, engine):
    """Full RFC 8628 happy path: authorize → pending → approve → token → consumed."""
    import jwt as _pyjwt

    # 1. Start device flow
    r = await client.post("/api/v1/auth/device")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "device_code" in body
    assert "user_code" in body
    assert "verification_uri" in body
    assert "expires_in" in body
    assert "interval" in body
    device_code = body["device_code"]
    user_code = body["user_code"]

    # 2. Poll before approve → authorization_pending
    r2 = await client.post("/api/v1/auth/device/token",
                           json={"device_code": device_code})
    assert r2.status_code == 400, r2.text
    assert r2.json()["error"] == "authorization_pending"

    # 3. Approve with a forged principal
    headers = _principal_headers(user_id=7, tenant_id=1, role="tenant_operator",
                                 secret="unit-secret")
    r3 = await client.post("/api/v1/auth/device/approve",
                           json={"user_code": user_code, "action": "approve"},
                           headers=headers)
    assert r3.status_code == 200, r3.text
    assert r3.json()["status"] == "approved"

    # 4. Poll → success; verify JWT carries approver identity
    r4 = await client.post("/api/v1/auth/device/token",
                           json={"device_code": device_code})
    assert r4.status_code == 200, r4.text
    tok_body = r4.json()
    assert "access_token" in tok_body
    assert tok_body["token_type"] == "Bearer"
    assert tok_body["expires_in"] == 3600
    claims = _pyjwt.decode(
        tok_body["access_token"], "unit-secret",
        algorithms=["HS256"],
        issuer="dlw-controller",
        options={"require": ["sub", "tid", "role"]},
    )
    assert claims["sub"] == "7"
    assert claims["tid"] == 1
    assert claims["role"] == "tenant_operator"

    # 5. Poll again → expired_token (consumed)
    r5 = await client.post("/api/v1/auth/device/token",
                           json={"device_code": device_code})
    assert r5.status_code == 400, r5.text
    assert r5.json()["error"] == "expired_token"


@pytest.mark.slow
async def test_slow_down(client):
    """Two back-to-back polls produce slow_down on the second."""
    r = await client.post("/api/v1/auth/device")
    dc = r.json()["device_code"]

    # First poll — authorization_pending (interval set, last_polled_at now)
    r1 = await client.post("/api/v1/auth/device/token",
                           json={"device_code": dc})
    assert r1.status_code == 400

    # Immediate second poll — slow_down
    r2 = await client.post("/api/v1/auth/device/token",
                           json={"device_code": dc})
    assert r2.status_code == 400, r2.text
    assert r2.json()["error"] == "slow_down"


@pytest.mark.slow
async def test_expired_token(client, engine):
    """Setting expires_at to past → poll returns expired_token."""
    import datetime

    from sqlalchemy import update

    from dlw.db.models.device_auth import DeviceAuthSession

    r = await client.post("/api/v1/auth/device")
    body = r.json()
    dc = body["device_code"]
    uc = body["user_code"]

    # Force-expire the row
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        await s.execute(
            update(DeviceAuthSession)
            .where(DeviceAuthSession.user_code == uc)
            .values(expires_at=datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc))
        )
        await s.commit()

    r2 = await client.post("/api/v1/auth/device/token",
                           json={"device_code": dc})
    assert r2.status_code == 400, r2.text
    assert r2.json()["error"] == "expired_token"


@pytest.mark.slow
async def test_access_denied(client):
    """Deny action → poll returns access_denied."""
    r = await client.post("/api/v1/auth/device")
    body = r.json()
    dc = body["device_code"]
    uc = body["user_code"]

    headers = _principal_headers(user_id=7, tenant_id=1, role="tenant_operator",
                                 secret="unit-secret")
    rd = await client.post("/api/v1/auth/device/approve",
                           json={"user_code": uc, "action": "deny"},
                           headers=headers)
    assert rd.status_code == 200
    assert rd.json()["status"] == "denied"

    r2 = await client.post("/api/v1/auth/device/token",
                           json={"device_code": dc})
    assert r2.status_code == 400, r2.text
    assert r2.json()["error"] == "access_denied"


@pytest.mark.slow
async def test_invalid_grant(client):
    """Unknown device_code → invalid_grant."""
    r = await client.post("/api/v1/auth/device/token",
                          json={"device_code": "bogus-code-that-does-not-exist"})
    assert r.status_code == 400, r.text
    assert r.json()["error"] == "invalid_grant"


@pytest.mark.slow
async def test_unknown_user_code_approve(client):
    """Approve a non-existent user_code → 404 DEVICE_CODE_INVALID."""
    headers = _principal_headers(user_id=7, tenant_id=1, role="tenant_operator",
                                 secret="unit-secret")
    r = await client.post("/api/v1/auth/device/approve",
                          json={"user_code": "XXXX-XXXX", "action": "approve"},
                          headers=headers)
    assert r.status_code == 404, r.text
    assert r.json()["detail"]["code"] == "DEVICE_CODE_INVALID"


@pytest.mark.slow
async def test_service_cannot_approve(service_client):
    """Service principal (system-admin token) → 403 SERVICE_CANNOT_APPROVE."""
    # First start a device flow with the service_client (no-auth endpoint)
    r = await service_client.post("/api/v1/auth/device")
    assert r.status_code == 200, r.text
    uc = r.json()["user_code"]

    r2 = await service_client.post(
        "/api/v1/auth/device/approve",
        json={"user_code": uc, "action": "approve"},
        headers=_service_headers("svc-tok"),
    )
    assert r2.status_code == 403, r2.text
    assert r2.json()["detail"]["code"] == "SERVICE_CANNOT_APPROVE"
