"""Shared SP4 fixtures: seeded test DB + real system-JWT + async SDK client.

R2: explicit __all__ — `from tests.sdk._fixtures import *` would otherwise
drop the underscore-prefixed autouse fixtures and nothing would be seeded."""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.config import get_settings
from dlw.db.base import Base

SECRET = "unit-secret"

__all__ = ["SECRET", "_bootstrap", "_set_token", "_patch_hf",
           "token", "app", "aclient"]


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(Tenant(id=1, slug="default", display_name="Default"))
        await s.flush()
        s.add(Project(id=1, tenant_id=1, name="default"))
        s.add(User(id=1, tenant_id=1, oidc_subject="dev",
                   email="d@l", role="tenant_admin"))
        s.add(StorageBackend(id=1, tenant_id=1, name="default",
                             backend_type="s3", config_encrypted=b""))
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _set_token(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _patch_hf(monkeypatch):
    from dlw.services.hf_metadata import RepoFile

    async def fake(*a, **k):
        return [
            RepoFile(path="config.json", size=4096, sha256=None),
            RepoFile(path="model.safetensors", size=64 * 1024,
                     sha256="a" * 64),
        ]
    monkeypatch.setattr("dlw.services.task_service.list_repo_tree", fake)


@pytest.fixture
def token() -> str:
    from dlw.auth.principal import issue_system_jwt
    return issue_system_jwt(secret=SECRET, user_id=1, tenant_id=1,
                            role="tenant_admin", project_ids=[])


@pytest.fixture
def app(ephemeral_ca):
    from tests.conftest import make_app_with_state
    return make_app_with_state(ephemeral_ca, enrollment_token="e")


@pytest_asyncio.fixture
async def aclient(app, token):
    """Async SDK client over the real ASGI app — same pattern/loop as
    tests/api/test_tasks.py's `client` fixture (proven in full suite)."""
    from dlw.sdk.aclient import AsyncClient
    async with AsyncClient(server="http://test", token=token,
                           transport=ASGITransport(app=app)) as c:
        yield c
