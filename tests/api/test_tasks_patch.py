"""PATCH /api/v1/tasks/{id} REST endpoint tests."""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.config import get_settings
from dlw.db.base import Base

TASK_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    import dlw.db.models  # noqa: F401
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.task import DownloadTask
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add(Tenant(id=1, slug="default", display_name="Default"))
        await s.flush()
        s.add(Project(id=1, tenant_id=1, name="d"))
        s.add(User(id=1, tenant_id=1, oidc_subject="u1", role="tenant_admin"))
        s.add(StorageBackend(id=1, tenant_id=1, name="s",
                              backend_type="s3", config_encrypted=b""))
        await s.flush()
        s.add(DownloadTask(
            id=TASK_ID, tenant_id=1, project_id=1, owner_user_id=1,
            storage_id=1, repo_id="org/m", revision="0" * 40,
            path_template="hf/{model}/{revision}/{file}",
            status="running", priority=1, source_strategy="auto_balance",
            source_blacklist=[]))
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


@pytest_asyncio.fixture
async def client(ephemeral_ca):
    from tests.conftest import make_app_with_state
    app = make_app_with_state(ephemeral_ca, enrollment_token="e")
    async with AsyncClient(transport=ASGITransport(app=app),
                            base_url="http://test") as c:
        yield c


def _token(role: str = "tenant_admin", user_id: int = 1, tenant_id: int = 1) -> str:
    from dlw.auth.principal import issue_system_jwt
    s = get_settings()
    return issue_system_jwt(secret=s.system_jwt_secret, user_id=user_id,
                             tenant_id=tenant_id, role=role, project_ids=[])


async def test_patch_priority_returns_200(client):
    r = await client.patch(f"/api/v1/tasks/{TASK_ID}",
                            json={"priority": 5},
                            headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 200
    assert r.json()["priority"] == 5


async def test_patch_strategy_returns_200(client, engine):
    # TaskRead doesn't expose source_strategy; check 200 + verify in DB.
    r = await client.patch(f"/api/v1/tasks/{TASK_ID}",
                            json={"source_strategy": "fastest_only"},
                            headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 200
    from dlw.db.models.task import DownloadTask
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        t = await s.get(DownloadTask, TASK_ID)
        assert t.source_strategy == "fastest_only"


async def test_patch_unauthenticated_returns_401(client):
    r = await client.patch(f"/api/v1/tasks/{TASK_ID}", json={"priority": 1})
    assert r.status_code == 401


async def test_patch_cross_tenant_returns_404(client):
    r = await client.patch(f"/api/v1/tasks/{TASK_ID}",
                            json={"priority": 1},
                            headers={"Authorization": f"Bearer {_token(tenant_id=999)}"})
    assert r.status_code == 404


async def test_patch_invalid_strategy_returns_422(client):
    r = await client.patch(f"/api/v1/tasks/{TASK_ID}",
                            json={"source_strategy": "evil"},
                            headers={"Authorization": f"Bearer {_token()}"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_PATCH"
