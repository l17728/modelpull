"""source-proxy routes to the assigned driver, INVARIANT 2 (SP2)."""
from __future__ import annotations

import uuid

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

import dlw.db.models  # noqa: F401
from dlw.db.base import Base
from tests.conftest import (
    executor_request_headers,
    make_app_with_state,
    register_test_executor,
)

pytestmark = pytest.mark.slow

SECRET = "unit-secret"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", SECRET)
    monkeypatch.setenv("DLW_TLS_TRUSTED_PROXY", "1")
    from dlw.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def app_client(ephemeral_ca, engine, monkeypatch):
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)
        await c.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Project, Tenant, User
    async with f() as s:
        s.add(Tenant(id=1, slug="t", display_name="T"))
        await s.flush()
        s.add_all([Project(id=1, tenant_id=1, name="d"),
                   User(id=1, tenant_id=1, oidc_subject="u", email="e",
                        role="tenant_operator"),
                   StorageBackend(id=1, tenant_id=1, name="s",
                                  backend_type="s3", config_encrypted=b"")])
        await s.commit()
    app = make_app_with_state(ephemeral_ca, enrollment_token="e")

    class _D:
        id = "modelscope"

        def download_url(self, file):
            return "https://www.modelscope.cn/x"

        def auth_token(self, t):
            from dlw.sources.base import SourceToken
            return SourceToken(scheme="none")

    class _Reg:
        def get(self, sid):
            return _D() if sid == "modelscope" else None

    app.state.source_registry = _Reg()
    import dlw.api.source_proxy as sp
    monkeypatch.setattr(sp, "_make_source_client", lambda _t: httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=b"HELLO",
                                     headers={"Content-Length": "5"}))))
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield app, c, f
    # Clean up so this fixture's seeded Tenant(id=1) etc. don't leak into a
    # later module's non-clean-slate _bootstrap (session-scoped DB).
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)


async def test_proxy_streams_from_assigned_source(app_client):
    app, client, f = app_client
    from dlw.db.models.task import DownloadTask, FileSubTask
    reg = await register_test_executor(client, enrollment_token="e")
    async with f() as s:
        t = DownloadTask(tenant_id=1, project_id=1, owner_user_id=1,
                         repo_id="o/r", revision="abc", storage_id=1,
                         path_template="t", status="downloading")
        s.add(t)
        await s.flush()
        tok = uuid.uuid4()
        sub = FileSubTask(task_id=t.id, tenant_id=1, filename="m",
                          file_size=5, status="assigned",
                          executor_id=reg["executor_id"],
                          executor_epoch=reg["epoch"], assignment_token=tok,
                          source_id="modelscope")
        s.add(sub)
        await s.commit()
        sub_id = sub.id
    h = {**executor_request_headers(reg), "X-Assignment-Token": str(tok)}
    r = await client.get(f"/api/v1/source-proxy/subtask/{sub_id}", headers=h)
    assert r.status_code == 200
    assert r.content == b"HELLO"
