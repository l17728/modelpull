"""Tests for dlw.services.task_service.create_task (Phase 1 W4: real HF)."""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.base import Base
from dlw.db.models.storage import StorageBackend
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.db.models.tenant import Project, Tenant, User
from dlw.schemas.task import TaskCreate
from dlw.services.hf_metadata import (
    HfNetworkError,
    HfPrivateOrAuthRequired,
    RepoFile,
    RepoNotFound,
)
from dlw.services.task_service import EmptyRepo, create_task


@pytest.fixture(scope="module", autouse=True)
async def _create_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def env(db_session: AsyncSession):
    """Tenant + project + user + storage fixtures (tenant_id=1 hardcoded for Phase 1)."""
    tenant = Tenant(id=1, slug="default", display_name="Default")
    db_session.add(tenant); await db_session.flush()
    project = Project(id=1, tenant_id=1, name="default")
    db_session.add(project)
    user = User(
        id=1, tenant_id=1, oidc_subject="dev-user", email="dev@local",
        role="tenant_admin",
    )
    db_session.add(user)
    sb = StorageBackend(
        id=1, tenant_id=1, name="default", backend_type="s3", config_encrypted=b"",
    )
    db_session.add(sb)
    await db_session.flush()


@pytest.fixture
def patch_hf(monkeypatch: pytest.MonkeyPatch):
    """Helper to monkeypatch list_repo_tree with a list of RepoFile."""
    def _patch(files: list[RepoFile] | Exception):
        async def fake(*args, **kwargs):
            if isinstance(files, Exception):
                raise files
            return list(files)
        monkeypatch.setattr(
            "dlw.services.task_service.list_repo_tree", fake
        )
    return _patch


@pytest.mark.slow
async def test_create_task_persists_subtasks_from_hf_response(
    db_session: AsyncSession, env, patch_hf,
) -> None:
    patch_hf([
        RepoFile(path="config.json", size=4096, sha256=None),
        RepoFile(path="model.safetensors", size=1_000_000_000, sha256="a" * 64),
        RepoFile(path="tokenizer.json", size=2_000_000, sha256="b" * 64),
    ])
    body = TaskCreate(
        repo_id="o/test",
        revision="0123456789abcdef" * 2 + "01234567",
        storage_id=1,
    )
    task = await create_task(
        db_session, body,
        owner_user_id=1, tenant_id=1, project_id=1,
        hf_endpoint="https://huggingface.co", hf_token=None,
    )
    assert task.status == "pending"

    subs = (await db_session.execute(
        select(FileSubTask).where(FileSubTask.task_id == task.id)
    )).scalars().all()
    assert len(subs) == 3
    by_name = {s.filename: s for s in subs}
    assert by_name["config.json"].file_size == 4096
    assert by_name["config.json"].expected_sha256 is None
    assert by_name["model.safetensors"].file_size == 1_000_000_000
    assert by_name["model.safetensors"].expected_sha256 == "a" * 64
    assert all(s.status == "pending" for s in subs)
    assert all(s.tenant_id == 1 for s in subs)


@pytest.mark.slow
async def test_create_task_relationship_populated(
    db_session: AsyncSession, env, patch_hf,
) -> None:
    """Existing W3 UI scaffold contract: DownloadTask.subtasks relationship."""
    from sqlalchemy.orm import selectinload

    patch_hf([
        RepoFile(path="a.bin", size=100, sha256=None),
        RepoFile(path="b.bin", size=200, sha256=None),
    ])
    body = TaskCreate(repo_id="o/r", revision="a" * 40, storage_id=1)
    task = await create_task(
        db_session, body, owner_user_id=1, tenant_id=1, project_id=1,
        hf_endpoint="https://huggingface.co", hf_token=None,
    )
    refreshed = (await db_session.execute(
        select(DownloadTask)
          .where(DownloadTask.id == task.id)
          .options(selectinload(DownloadTask.subtasks))
    )).scalar_one()
    assert {s.filename for s in refreshed.subtasks} == {"a.bin", "b.bin"}


@pytest.mark.slow
async def test_create_task_raises_EmptyRepo_when_hf_returns_zero_files(
    db_session: AsyncSession, env, patch_hf,
) -> None:
    patch_hf([])
    body = TaskCreate(repo_id="o/empty", revision="a" * 40, storage_id=1)
    with pytest.raises(EmptyRepo):
        await create_task(
            db_session, body, owner_user_id=1, tenant_id=1, project_id=1,
            hf_endpoint="https://huggingface.co", hf_token=None,
        )


@pytest.mark.slow
async def test_create_task_propagates_RepoNotFound(
    db_session: AsyncSession, env, patch_hf,
) -> None:
    patch_hf(RepoNotFound("repo o/missing not found"))
    body = TaskCreate(repo_id="o/missing", revision="a" * 40, storage_id=1)
    with pytest.raises(RepoNotFound):
        await create_task(
            db_session, body, owner_user_id=1, tenant_id=1, project_id=1,
            hf_endpoint="https://huggingface.co", hf_token=None,
        )


@pytest.mark.slow
async def test_create_task_propagates_HfPrivateOrAuthRequired(
    db_session: AsyncSession, env, patch_hf,
) -> None:
    patch_hf(HfPrivateOrAuthRequired("private"))
    body = TaskCreate(repo_id="o/private", revision="a" * 40, storage_id=1)
    with pytest.raises(HfPrivateOrAuthRequired):
        await create_task(
            db_session, body, owner_user_id=1, tenant_id=1, project_id=1,
            hf_endpoint="https://huggingface.co", hf_token=None,
        )


@pytest.mark.slow
async def test_create_task_propagates_HfNetworkError(
    db_session: AsyncSession, env, patch_hf,
) -> None:
    patch_hf(HfNetworkError("dns"))
    body = TaskCreate(repo_id="o/x", revision="a" * 40, storage_id=1)
    with pytest.raises(HfNetworkError):
        await create_task(
            db_session, body, owner_user_id=1, tenant_id=1, project_id=1,
            hf_endpoint="https://huggingface.co", hf_token=None,
        )
