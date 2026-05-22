"""Tests that the executor /poll payload carries subtask_chunks rows (SP2)."""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from dlw.config import get_settings
from dlw.db.base import Base
from dlw.db.models.source import SubtaskChunk
from dlw.db.models.task import DownloadTask, FileSubTask
from tests.conftest import (
    executor_request_headers,
    make_app_with_state,
    register_test_executor,
    signed_heartbeat_headers,
)

SECRET = "unit-secret-pc"
_ENROLL = "test-enrollment-token-poll-chunks"


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    """Create tables + seed default tenant/project/user/storage."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Project, Tenant, User

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        # Use distinct IDs (10x) to avoid conflicts with test_executors.py module
        s.add(Tenant(id=10, slug="pc", display_name="PC"))
        await s.flush()
        s.add(Project(id=10, tenant_id=10, name="pc"))
        s.add(User(id=10, tenant_id=10, oidc_subject="pc",
                   email="pc@l", role="tenant_admin"))
        s.add(StorageBackend(id=10, tenant_id=10, name="pc",
                              backend_type="s3", config_encrypted=b""))
        await s.commit()
    yield


@pytest.fixture(autouse=True)
def _set_token(monkeypatch: pytest.MonkeyPatch):
    get_settings.cache_clear()
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", SECRET)
    monkeypatch.setenv("DLW_TLS_TRUSTED_PROXY", "1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def client(ephemeral_ca):
    app = make_app_with_state(ephemeral_ca, enrollment_token=_ENROLL)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _register_and_heartbeat(client, executor_id: str, host_id: str) -> dict:
    reg = await register_test_executor(
        client, enrollment_token=_ENROLL,
        executor_id=executor_id, host_id=host_id,
    )
    hb_body = b'{"health_score": 100, "parts_dir_bytes": 0, "disk_free_gb": 1000}'
    r = await client.post(
        f"/api/v1/executors/{executor_id}/heartbeat",
        content=hb_body,
        headers=signed_heartbeat_headers(reg, hb_body),
    )
    assert r.status_code == 200, r.text
    return reg


async def _seed_task_and_subtask(
    db_session,
    *,
    is_chunked: bool,
    file_size: int = 134_217_728,  # 128 MiB
) -> FileSubTask:
    """Insert a DownloadTask + FileSubTask (pending) into the DB and commit."""
    task_id = uuid.uuid4()
    sub_id = uuid.uuid4()

    task = DownloadTask(
        id=task_id,
        tenant_id=10,
        project_id=10,
        owner_user_id=10,
        repo_id=f"org/repo-{sub_id.hex[:8]}",
        revision="a" * 40,
        storage_id=10,
        path_template="{repo}/{revision}/{filename}",
        priority=1,
        status="downloading",
    )
    db_session.add(task)
    await db_session.flush()

    sub = FileSubTask(
        id=sub_id,
        task_id=task_id,
        tenant_id=10,
        filename=f"model-{sub_id.hex[:8]}.bin",
        file_size=file_size,
        status="pending",
        is_chunked=is_chunked,
    )
    db_session.add(sub)
    await db_session.flush()

    return sub


@pytest.mark.slow
async def test_poll_carries_chunk_rows(client, db_session) -> None:
    """Chunked subtask: /poll response must include 2 chunk rows."""
    file_size = 134_217_728  # 128 MiB

    sub = await _seed_task_and_subtask(db_session, is_chunked=True, file_size=file_size)

    chunk0 = SubtaskChunk(
        subtask_id=sub.id,
        chunk_index=0,
        byte_start=0,
        byte_end=67_108_863,
        source_id="modelscope",
        status="pending",
    )
    chunk1 = SubtaskChunk(
        subtask_id=sub.id,
        chunk_index=1,
        byte_start=67_108_864,
        byte_end=file_size - 1,
        source_id="hf_mirror",
        status="pending",
    )
    db_session.add(chunk0)
    db_session.add(chunk1)
    await db_session.commit()

    reg = await _register_and_heartbeat(client, "exec-chunks-1", "host-chunks-1")
    resp = await client.post(
        f"/api/v1/executors/exec-chunks-1/poll",
        headers=executor_request_headers(reg),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["assigned"] is True
    chunks = body["subtask"]["chunks"]
    assert [c["chunk_index"] for c in chunks] == [0, 1]
    assert chunks[0]["byte_start"] == 0
    assert chunks[0]["source_id"] == "modelscope"
    assert chunks[1]["byte_start"] == 67_108_864
    assert chunks[1]["source_id"] == "hf_mirror"


@pytest.mark.slow
async def test_poll_non_chunked_subtask_has_empty_chunks(client, db_session) -> None:
    """Non-chunked subtask: /poll response must have chunks == []."""
    sub = await _seed_task_and_subtask(db_session, is_chunked=False)
    await db_session.commit()

    reg = await _register_and_heartbeat(client, "exec-nochunks-1", "host-nochunks-1")
    resp = await client.post(
        f"/api/v1/executors/exec-nochunks-1/poll",
        headers=executor_request_headers(reg),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["assigned"] is True
    assert body["subtask"]["chunks"] == []
