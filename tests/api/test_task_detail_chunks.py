"""Tests for GET /api/v1/tasks/{id}/subtask-chunks (UI-SP2)."""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.config import get_settings
from dlw.db.base import Base
from tests.conftest import make_app_with_state, principal_headers

SECRET = "unit-secret"


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(Tenant(id=1, slug="default", display_name="Default"))
        await session.flush()
        session.add(Project(id=1, tenant_id=1, name="default"))
        session.add(User(id=1, tenant_id=1, oidc_subject="dev",
                         email="d@l", role="tenant_admin"))
        session.add(StorageBackend(id=1, tenant_id=1, name="default",
                                   backend_type="s3", config_encrypted=b""))
        await session.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _set_token(monkeypatch: pytest.MonkeyPatch):
    get_settings.cache_clear()
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _patch_hf(monkeypatch: pytest.MonkeyPatch):
    from dlw.services.hf_metadata import RepoFile

    async def fake(*args, **kwargs):
        return [
            RepoFile(path="config.json", size=4096, sha256=None),
            RepoFile(path="model.safetensors", size=64 * 1024, sha256="a" * 64),
        ]
    monkeypatch.setattr("dlw.services.task_service.list_repo_tree", fake)


@pytest.fixture
def auth() -> dict[str, str]:
    return principal_headers(secret=SECRET, role="tenant_admin")


@pytest.fixture
async def client(ephemeral_ca):
    app = make_app_with_state(ephemeral_ca, enrollment_token="e")
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c


async def _make_task(client, auth) -> str:
    r = await client.post("/api/v1/tasks", json={
        "repo_id": "o/chunks", "revision": "3" * 40, "storage_id": 1,
    }, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.mark.slow
async def test_subtask_chunks_unauthenticated_401(client: AsyncClient) -> None:
    r = await client.get(f"/api/v1/tasks/{uuid.uuid4()}/subtask-chunks")
    assert r.status_code == 401


@pytest.mark.slow
async def test_subtask_chunks_cross_tenant_404(client: AsyncClient, auth) -> None:
    tid = await _make_task(client, auth)
    other = principal_headers(secret=SECRET, role="tenant_admin",
                              user_id=9, tenant_id=2)
    r = await client.get(f"/api/v1/tasks/{tid}/subtask-chunks", headers=other)
    assert r.status_code == 404


@pytest.mark.slow
async def test_subtask_chunks_happy_and_aggregation(
    client: AsyncClient, auth, engine,
) -> None:
    tid = await _make_task(client, auth)
    from dlw.db.models.source import SubtaskChunk
    from dlw.db.models.task import FileSubTask
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        subs = (await s.execute(
            select(FileSubTask).where(FileSubTask.task_id == uuid.UUID(tid))
            .order_by(FileSubTask.filename))).scalars().all()
        assert len(subs) == 2
        big = subs[1]
        big.is_chunked = True
        big.file_size = 1000
        big.bytes_downloaded = 600
        s.add(SubtaskChunk(subtask_id=big.id, chunk_index=0, byte_start=0,
                           byte_end=499, source_id="hf", status="succeeded",
                           bytes_done=500))
        s.add(SubtaskChunk(subtask_id=big.id, chunk_index=1, byte_start=500,
                           byte_end=999, source_id="modelscope",
                           status="pending", bytes_done=100))
        await s.commit()

    r = await client.get(f"/api/v1/tasks/{tid}/subtask-chunks", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["items"]) == 2
    by_name = {i["filename"]: i for i in body["items"]}
    chunked = by_name["model.safetensors"]
    assert chunked["is_chunked"] is True
    assert len(chunked["chunks"]) == 2
    assert chunked["chunks"][0]["source_id"] == "hf"
    assert chunked["chunks"][1]["bytes_done"] == 100
    assert by_name["config.json"]["chunks"] == []
