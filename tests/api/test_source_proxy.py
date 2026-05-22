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


async def test_proxy_routes_chunked_range_to_chunk_source(app_client):
    """Full 2-source test: a chunked subtask with 2 SubtaskChunk rows pointing
    at different source_ids.  A Range that lands in chunk-0's byte range must
    be served from source A's upstream; one that lands in chunk-1's range must
    be served from source B's upstream.  Proves the byte_start<=start<=byte_end
    → chunk.source_id routing at the source-proxy level (SP2 chunk alignment)."""
    app, client, f = app_client
    from dlw.db.models.source import SubtaskChunk
    from dlw.db.models.task import DownloadTask, FileSubTask

    # Two fake drivers that return different body bytes so we can tell which
    # upstream actually handled the request.
    served_by: list[str] = []

    def _mock_transport(label: str):
        def _handler(r: httpx.Request) -> httpx.Response:
            served_by.append(label)
            return httpx.Response(206, content=label.encode(),
                                  headers={"Content-Length": str(len(label)),
                                           "Content-Range": "bytes 0-0/1"})
        return httpx.MockTransport(_handler)

    class _DriverA:
        id = "source_a"

        def download_url(self, file):
            return "https://source-a.example.com/file"

        def auth_token(self, t):
            from dlw.sources.base import SourceToken
            return SourceToken(scheme="none")

    class _DriverB:
        id = "source_b"

        def download_url(self, file):
            return "https://source-b.example.com/file"

        def auth_token(self, t):
            from dlw.sources.base import SourceToken
            return SourceToken(scheme="none")

    # Registry knows both drivers.
    class _TwoSourceReg:
        def get(self, sid):
            if sid == "source_a":
                return _DriverA()
            if sid == "source_b":
                return _DriverB()
            return None

    app.state.source_registry = _TwoSourceReg()

    reg = await register_test_executor(client, enrollment_token="e")

    # Seed: one chunked FileSubTask with 2 SubtaskChunk rows on different sources.
    # chunk-0: bytes 0–499 → source_a
    # chunk-1: bytes 500–999 → source_b
    async with f() as s:
        t = DownloadTask(tenant_id=1, project_id=1, owner_user_id=1,
                         repo_id="o/r", revision="abc", storage_id=1,
                         path_template="t", status="downloading")
        s.add(t)
        await s.flush()
        tok = uuid.uuid4()
        sub = FileSubTask(task_id=t.id, tenant_id=1, filename="big.bin",
                          file_size=1000, status="assigned",
                          executor_id=reg["executor_id"],
                          executor_epoch=reg["epoch"], assignment_token=tok,
                          source_id="source_a", is_chunked=True)
        s.add(sub)
        await s.flush()
        s.add(SubtaskChunk(subtask_id=sub.id, chunk_index=0,
                            byte_start=0, byte_end=499,
                            source_id="source_a", status="pending"))
        s.add(SubtaskChunk(subtask_id=sub.id, chunk_index=1,
                            byte_start=500, byte_end=999,
                            source_id="source_b", status="pending"))
        await s.commit()
        sub_id = sub.id

    import dlw.api.source_proxy as sp

    h = {**executor_request_headers(reg), "X-Assignment-Token": str(tok)}

    # --- Range in chunk-0 → must be served by source_a ---
    served_by.clear()
    with httpx.Client() as _dummy:
        pass  # ensure no state carryover

    import unittest.mock as _mock

    # Patch _make_source_client per-call so we can use different transports.
    original_make = sp._make_source_client

    def _patched_make(timeout):
        # Identify which source is being called by inspecting served_by length
        # at call time — simpler: return a transport that records the source
        # from the URL hostname.
        def _handler(r: httpx.Request) -> httpx.Response:
            label = "source_a" if "source-a" in str(r.url) else "source_b"
            served_by.append(label)
            return httpx.Response(206, content=label.encode(),
                                  headers={"Content-Length": str(len(label)),
                                           "Content-Range": "bytes 0-0/1"})
        return httpx.AsyncClient(transport=httpx.MockTransport(_handler))

    with _mock.patch.object(sp, "_make_source_client", _patched_make):
        # chunk-0 range: bytes=0-499 → source_a
        served_by.clear()
        r0 = await client.get(f"/api/v1/source-proxy/subtask/{sub_id}",
                               headers={**h, "Range": "bytes=0-499"})
        assert r0.status_code == 206
        assert served_by == ["source_a"], (
            f"Expected source_a for bytes=0-499, got {served_by}")

        # chunk-1 range: bytes=500-999 → source_b
        served_by.clear()
        r1 = await client.get(f"/api/v1/source-proxy/subtask/{sub_id}",
                               headers={**h, "Range": "bytes=500-999"})
        assert r1.status_code == 206
        assert served_by == ["source_b"], (
            f"Expected source_b for bytes=500-999, got {served_by}")
