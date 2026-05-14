"""W3b: GET /api/v1/hf-proxy/subtask/{id} — controller-side HF reverse proxy."""
from __future__ import annotations

import uuid

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.config import get_settings
from dlw.db.base import Base
from dlw.db.models.task import FileSubTask
from tests.conftest import (
    executor_request_headers, register_test_executor, signed_heartbeat_headers,
)

_TOKEN = "hf-proxy-ui-token"
_ENROLL = "hf-proxy-enrollment-token"
_HF_TOKEN = "test-hf-token-xyz"


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    """Tables + minimal seed (tenant + project + user + storage)."""
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(Tenant(id=1, slug="d", display_name="D"))
        await s.flush()
        s.add(Project(id=1, tenant_id=1, name="d"))
        s.add(User(id=1, tenant_id=1, oidc_subject="d",
                   email="d@l", role="tenant_admin"))
        s.add(StorageBackend(id=1, tenant_id=1, name="d",
                             backend_type="s3", config_encrypted=b""))
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
async def _cleanup_tasks(engine):
    """Truncate task rows after each test so a later test's poll cannot claim a
    prior test's leftover subtask (matches tests/api/test_subtasks.py)."""
    yield
    from sqlalchemy import text
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE file_subtasks, download_tasks CASCADE"))


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DLW_BEARER_TOKEN", _TOKEN)
    monkeypatch.setenv("DLW_TLS_TRUSTED_PROXY", "1")
    monkeypatch.setenv("DLW_HF_TOKEN", _HF_TOKEN)
    monkeypatch.setenv("DLW_HF_ENDPOINT", "https://huggingface.co")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def proxy_app(ephemeral_ca):
    from dlw.auth.hmac_nonce import NonceStore
    from dlw.main import create_app
    app = create_app()
    app.state.ca = ephemeral_ca["ca"]
    app.state.jwt_keypair = ephemeral_ca["jwt_keypair"]
    app.state.nonce_store = NonceStore(maxsize=1000, ttl_seconds=300)
    app.state.enrollment_token = _ENROLL
    return app


def _install_hf_mock(monkeypatch, handler):
    """Point the proxy's HF client factory at an httpx.MockTransport(handler)."""
    import dlw.api.hf_proxy as hf_proxy_mod

    def _fake_make_hf_client(timeout_seconds):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True,
        )
    monkeypatch.setattr(hf_proxy_mod, "_make_hf_client", _fake_make_hf_client)


async def _create_task_and_claim(c, reg):
    """POST a task (2 files via the _patch_hf_global autouse fixture), heartbeat
    once to flip the executor joining->healthy (the scheduler only assigns work
    to healthy/degraded executors), poll once as `reg`, and return
    (subtask_id, assignment_token, filename)."""
    r = await c.post("/api/v1/tasks", json={
        "repo_id": "deepseek-ai/DeepSeek-V3",
        "revision": "a" * 40,
        "storage_id": 1,
    }, headers={"Authorization": f"Bearer {_TOKEN}"})
    assert r.status_code == 201, r.text
    hb_body = b'{"health_score": 100, "parts_dir_bytes": 0, "disk_free_gb": 100}'
    r = await c.post(f"/api/v1/executors/{reg['executor_id']}/heartbeat",
                     content=hb_body,
                     headers=signed_heartbeat_headers(reg, hb_body))
    assert r.status_code == 200, r.text
    r = await c.post(f"/api/v1/executors/{reg['executor_id']}/poll",
                     headers=executor_request_headers(reg))
    assert r.status_code == 200, r.text
    assert r.json()["assigned"] is True
    sub = r.json()["subtask"]
    return sub["id"], r.json()["assignment_token"], sub["filename"]


@pytest.mark.slow
async def test_proxy_streams_file_with_token_injected(
    proxy_app, monkeypatch,
) -> None:
    seen: dict = {}

    def hf_handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, content=b"PROXIED-BYTES")
    _install_hf_mock(monkeypatch, hf_handler)

    async with AsyncClient(transport=ASGITransport(app=proxy_app),
                           base_url="http://test") as c:
        reg = await register_test_executor(
            c, enrollment_token=_ENROLL,
            executor_id="hp-worker-1", host_id="hp-host",
        )
        sub_id, token, _ = await _create_task_and_claim(c, reg)
        r = await c.get(f"/api/v1/hf-proxy/subtask/{sub_id}", headers={
            **executor_request_headers(reg),
            "X-Assignment-Token": token,
        })

    assert r.status_code == 200, r.text
    assert r.content == b"PROXIED-BYTES"
    assert seen["auth"] == f"Bearer {_HF_TOKEN}"


@pytest.mark.slow
async def test_proxy_forwards_range_header(proxy_app, monkeypatch) -> None:
    seen: dict = {}

    def hf_handler(request: httpx.Request) -> httpx.Response:
        seen["range"] = request.headers.get("range")
        return httpx.Response(206, content=b"RANGE",
                              headers={"Content-Range": "bytes 0-4/100"})
    _install_hf_mock(monkeypatch, hf_handler)

    async with AsyncClient(transport=ASGITransport(app=proxy_app),
                           base_url="http://test") as c:
        reg = await register_test_executor(
            c, enrollment_token=_ENROLL,
            executor_id="hp-worker-2", host_id="hp-host",
        )
        sub_id, token, _ = await _create_task_and_claim(c, reg)
        r = await c.get(f"/api/v1/hf-proxy/subtask/{sub_id}", headers={
            **executor_request_headers(reg),
            "X-Assignment-Token": token,
            "Range": "bytes=0-4",
        })

    assert r.status_code == 206
    assert seen["range"] == "bytes=0-4"
    assert r.headers["content-range"] == "bytes 0-4/100"


@pytest.mark.slow
async def test_proxy_reconstructs_url_from_subtask(proxy_app, monkeypatch) -> None:
    seen: dict = {}

    def hf_handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, content=b"OK")
    _install_hf_mock(monkeypatch, hf_handler)

    async with AsyncClient(transport=ASGITransport(app=proxy_app),
                           base_url="http://test") as c:
        reg = await register_test_executor(
            c, enrollment_token=_ENROLL,
            executor_id="hp-worker-3", host_id="hp-host",
        )
        sub_id, token, filename = await _create_task_and_claim(c, reg)
        r = await c.get(f"/api/v1/hf-proxy/subtask/{sub_id}", headers={
            **executor_request_headers(reg),
            "X-Assignment-Token": token,
        })

    assert r.status_code == 200
    expected = (f"https://huggingface.co/deepseek-ai/DeepSeek-V3"
                f"/resolve/{'a' * 40}/{filename}")
    assert seen["url"] == expected


@pytest.mark.slow
async def test_proxy_forwards_hf_429(proxy_app, monkeypatch) -> None:
    def hf_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=b"rate limited")
    _install_hf_mock(monkeypatch, hf_handler)

    async with AsyncClient(transport=ASGITransport(app=proxy_app),
                           base_url="http://test") as c:
        reg = await register_test_executor(
            c, enrollment_token=_ENROLL,
            executor_id="hp-worker-4", host_id="hp-host",
        )
        sub_id, token, _ = await _create_task_and_claim(c, reg)
        r = await c.get(f"/api/v1/hf-proxy/subtask/{sub_id}", headers={
            **executor_request_headers(reg),
            "X-Assignment-Token": token,
        })

    assert r.status_code == 429


@pytest.mark.slow
async def test_proxy_rejects_unauthenticated(proxy_app) -> None:
    async with AsyncClient(transport=ASGITransport(app=proxy_app),
                           base_url="http://test") as c:
        r = await c.get(f"/api/v1/hf-proxy/subtask/{uuid.uuid4()}", headers={
            "X-Assignment-Token": str(uuid.uuid4()),
        })
    assert r.status_code == 401


@pytest.mark.slow
async def test_proxy_404_on_missing_subtask(proxy_app) -> None:
    async with AsyncClient(transport=ASGITransport(app=proxy_app),
                           base_url="http://test") as c:
        reg = await register_test_executor(
            c, enrollment_token=_ENROLL,
            executor_id="hp-worker-5", host_id="hp-host",
        )
        r = await c.get(f"/api/v1/hf-proxy/subtask/{uuid.uuid4()}", headers={
            **executor_request_headers(reg),
            "X-Assignment-Token": str(uuid.uuid4()),
        })
    assert r.status_code == 404


@pytest.mark.slow
async def test_proxy_rejects_not_your_subtask(proxy_app) -> None:
    """Authenticated executor B cannot proxy-fetch executor A's subtask."""
    async with AsyncClient(transport=ASGITransport(app=proxy_app),
                           base_url="http://test") as c:
        reg_a = await register_test_executor(
            c, enrollment_token=_ENROLL,
            executor_id="hp-owner-a", host_id="hp-host",
        )
        reg_b = await register_test_executor(
            c, enrollment_token=_ENROLL,
            executor_id="hp-owner-b", host_id="hp-host",
        )
        sub_id, token, _ = await _create_task_and_claim(c, reg_a)
        r = await c.get(f"/api/v1/hf-proxy/subtask/{sub_id}", headers={
            **executor_request_headers(reg_b),
            "X-Assignment-Token": token,
        })
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "NOT_YOUR_SUBTASK"


@pytest.mark.slow
async def test_proxy_rejects_stale_assignment_token(proxy_app) -> None:
    async with AsyncClient(transport=ASGITransport(app=proxy_app),
                           base_url="http://test") as c:
        reg = await register_test_executor(
            c, enrollment_token=_ENROLL,
            executor_id="hp-worker-stale", host_id="hp-host",
        )
        sub_id, _token, _ = await _create_task_and_claim(c, reg)
        r = await c.get(f"/api/v1/hf-proxy/subtask/{sub_id}", headers={
            **executor_request_headers(reg),
            "X-Assignment-Token": str(uuid.uuid4()),   # wrong token
        })
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "STALE_ASSIGNMENT"


@pytest.mark.slow
async def test_proxy_rejects_epoch_mismatch(proxy_app, db_session) -> None:
    async with AsyncClient(transport=ASGITransport(app=proxy_app),
                           base_url="http://test") as c:
        reg = await register_test_executor(
            c, enrollment_token=_ENROLL,
            executor_id="hp-worker-epoch", host_id="hp-host",
        )
        sub_id, token, _ = await _create_task_and_claim(c, reg)
        # Corrupt the subtask's stamped epoch so it no longer matches the
        # authenticated executor's current epoch.
        await db_session.execute(
            update(FileSubTask)
            .where(FileSubTask.id == uuid.UUID(sub_id))
            .values(executor_epoch=99_999)
        )
        await db_session.commit()
        r = await c.get(f"/api/v1/hf-proxy/subtask/{sub_id}", headers={
            **executor_request_headers(reg),
            "X-Assignment-Token": token,
        })
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "EPOCH_MISMATCH"
