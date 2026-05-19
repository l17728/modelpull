"""DELETE /api/v1/tasks/{id} — terminal-only, tenant-scoped, deref (SP3)."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import dlw.db.models  # noqa: F401
from dlw.config import get_settings
from dlw.db.base import Base
from tests.conftest import make_app_with_state, principal_headers

pytestmark = pytest.mark.slow
SECRET = "unit-secret"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def app_client(ephemeral_ca, engine):
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.storage_object import StorageObject, SubtaskObjectRef
    from dlw.db.models.task import DownloadTask, FileSubTask
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)
        await c.run_sync(Base.metadata.create_all)
    fac = async_sessionmaker(engine, expire_on_commit=False)
    async with fac() as s:
        s.add_all([Tenant(id=1, slug="t1", display_name="T1"),
                   Tenant(id=2, slug="t2", display_name="T2")])
        await s.flush()
        s.add_all([Project(id=1, tenant_id=1, name="d"),
                   User(id=1, tenant_id=1, oidc_subject="u", email="e",
                        role="tenant_admin"),
                   StorageBackend(id=1, tenant_id=1, name="s",
                                  backend_type="s3", config_encrypted=b"")])
        await s.flush()
        obj = StorageObject(tenant_id=1, storage_id=1, storage_key="k",
                            sha256="a" * 64, size=10, refcount=1)
        s.add(obj)
        t = DownloadTask(tenant_id=1, project_id=1, owner_user_id=1,
                         repo_id="o/r", revision="abc", storage_id=1,
                         path_template="t", status="succeeded")
        s.add(t)
        await s.flush()
        sub = FileSubTask(task_id=t.id, tenant_id=1, filename="m",
                          file_size=10, status="succeeded")
        s.add(sub)
        await s.flush()
        s.add(SubtaskObjectRef(subtask_id=sub.id, object_id=obj.id))
        active = DownloadTask(tenant_id=1, project_id=1, owner_user_id=1,
                              repo_id="o/r2", revision="abc", storage_id=1,
                              path_template="t", status="downloading")
        s.add(active)
        await s.flush()
        ids = {"done": t.id, "active": active.id}
        await s.commit()
    app = make_app_with_state(ephemeral_ca, enrollment_token="e")
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c, fac, ids
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _h(tid=1):
    return principal_headers(secret=SECRET, user_id=1, tenant_id=tid,
                             role="tenant_admin")


async def test_delete_terminal_decrements_refcount(app_client):
    c, fac, ids = app_client
    r = await c.delete(f"/api/v1/tasks/{ids['done']}", headers=_h())
    assert r.status_code == 204, r.text
    async with fac() as s:
        from dlw.db.models.storage_object import StorageObject
        obj = (await s.execute(select(StorageObject))).scalar_one()
        assert obj.refcount == 0
        from dlw.db.models.task import DownloadTask
        assert await s.get(DownloadTask, ids["done"]) is None


async def test_delete_non_terminal_409(app_client):
    c, fac, ids = app_client
    r = await c.delete(f"/api/v1/tasks/{ids['active']}", headers=_h())
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "TASK_NOT_TERMINAL"


async def test_delete_cross_tenant_404(app_client):
    c, fac, ids = app_client
    r = await c.delete(f"/api/v1/tasks/{ids['done']}", headers=_h(tid=2))
    assert r.status_code == 404


async def test_delete_unauth_401(app_client):
    c, fac, ids = app_client
    assert (await c.delete(f"/api/v1/tasks/{ids['done']}")).status_code == 401
