"""v2.1 Sprint 4 — /api/v1/replication REST tests."""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.config import get_settings
from dlw.db.base import Base


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    import dlw.db.models  # noqa: F401
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.storage_object import StorageObject
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add(Tenant(id=700, slug="repx", display_name="RepX"))
        await s.flush()
        s.add(Project(id=700, tenant_id=700, name="d"))
        s.add(User(id=700, tenant_id=700, oidc_subject="u700",
                    role="tenant_admin"))
        s.add(StorageBackend(id=700, tenant_id=700, name="src",
                              backend_type="s3", config_encrypted=b""))
        s.add(StorageBackend(id=701, tenant_id=700, name="dst",
                              backend_type="s3", config_encrypted=b""))
        await s.flush()
        s.add(StorageObject(
            id=7000, tenant_id=700, storage_id=700, storage_key="k7000",
            sha256="a" * 64, size=1024, refcount=1))
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setenv("DLW_AUTH_DEV_MODE", "true")
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", "unit-secret-32-bytes-pad-pad!!")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_jobs(engine):
    """Clear replication_jobs between tests so the partial unique
    (source, target) doesn't fire on subsequent creates of the same row."""
    yield
    from sqlalchemy import delete as _del
    from dlw.db.models.replication import ReplicationJob
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        await s.execute(_del(ReplicationJob))
        await s.commit()


@pytest_asyncio.fixture
async def client(ephemeral_ca):
    from tests.conftest import make_app_with_state
    app = make_app_with_state(ephemeral_ca, enrollment_token="e")
    async with AsyncClient(transport=ASGITransport(app=app),
                            base_url="http://test") as c:
        yield c


def _token(role: str = "tenant_admin", user_id: int = 700,
           tenant_id: int = 700) -> str:
    from dlw.auth.principal import issue_system_jwt
    s = get_settings()
    return issue_system_jwt(secret=s.system_jwt_secret, user_id=user_id,
                             tenant_id=tenant_id, role=role, project_ids=[])


async def test_create_job_201(client):
    r = await client.post(
        "/api/v1/replication",
        json={"source_object_id": 7000, "target_storage_id": 701},
        headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "pending"
    assert body["source_object_id"] == 7000
    assert body["target_storage_id"] == 701


async def test_create_unauth_401(client):
    r = await client.post(
        "/api/v1/replication",
        json={"source_object_id": 7000, "target_storage_id": 701})
    assert r.status_code == 401


async def test_create_unknown_source_404(client):
    r = await client.post(
        "/api/v1/replication",
        json={"source_object_id": 999999, "target_storage_id": 701},
        headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 404


async def test_create_same_storage_422(client):
    r = await client.post(
        "/api/v1/replication",
        json={"source_object_id": 7000, "target_storage_id": 700},
        headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_TARGET"


async def test_list_returns_only_own_tenant(client):
    # Create a job under tenant 700 via REST
    await client.post(
        "/api/v1/replication",
        json={"source_object_id": 7000, "target_storage_id": 701},
        headers={"Authorization": f"Bearer {_token()}"})
    # Another tenant calling list — should see no rows from tenant 700.
    r = await client.get(
        "/api/v1/replication",
        headers={"Authorization": f"Bearer {_token(tenant_id=999)}"})
    assert r.status_code == 200
    body = r.json()
    assert all(j["tenant_id"] == 999 for j in body["items"])


async def test_cancel_pending_job_200(client):
    r1 = await client.post(
        "/api/v1/replication",
        json={"source_object_id": 7000, "target_storage_id": 701},
        headers={"Authorization": f"Bearer {_token()}"})
    job_id = r1.json()["id"]
    r2 = await client.post(
        f"/api/v1/replication/{job_id}/cancel",
        headers={"Authorization": f"Bearer {_token()}"})
    assert r2.status_code == 200
    assert r2.json()["status"] == "cancelled"


async def test_get_one_404_for_cross_tenant(client):
    r1 = await client.post(
        "/api/v1/replication",
        json={"source_object_id": 7000, "target_storage_id": 701},
        headers={"Authorization": f"Bearer {_token()}"})
    job_id = r1.json()["id"]
    r2 = await client.get(
        f"/api/v1/replication/{job_id}",
        headers={"Authorization": f"Bearer {_token(tenant_id=999)}"})
    assert r2.status_code == 404
