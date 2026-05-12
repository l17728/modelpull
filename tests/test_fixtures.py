"""Tests for dlw.fixtures seed module (Phase 1 W5)."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.base import Base
from dlw.db.models.storage import StorageBackend
from dlw.db.models.task import DownloadTask
from dlw.db.models.tenant import Project, Tenant, User
from dlw.fixtures import StorageSeed, seed_default, seed_demo_data


@pytest.fixture(scope="module", autouse=True)
async def _create_tables(engine):
    """Create schema for the module; drop at module end (Phase 1 W5 W6-M discipline)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.mark.slow
async def test_seed_default_inserts_four_rows(db_session: AsyncSession) -> None:
    """seed_default creates exactly 1 tenant + 1 project + 1 user + 1 storage."""
    await seed_default(db_session)
    await db_session.flush()

    assert await db_session.scalar(select(func.count()).select_from(Tenant)) == 1
    assert await db_session.scalar(select(func.count()).select_from(Project)) == 1
    assert await db_session.scalar(select(func.count()).select_from(User)) == 1
    assert await db_session.scalar(select(func.count()).select_from(StorageBackend)) == 1


@pytest.mark.slow
async def test_seed_default_idempotent_via_on_conflict(
    db_session: AsyncSession,
) -> None:
    """Calling seed_default twice is a no-op (ON CONFLICT DO NOTHING)."""
    await seed_default(db_session)
    await db_session.flush()
    await seed_default(db_session)        # second call must not error
    await db_session.flush()
    # Still exactly 1 row per table
    assert await db_session.scalar(select(func.count()).select_from(Tenant)) == 1
    assert await db_session.scalar(select(func.count()).select_from(StorageBackend)) == 1


@pytest.mark.slow
async def test_seed_default_with_custom_storage_config(
    db_session: AsyncSession,
) -> None:
    """StorageSeed(config={...}) → config_encrypted contains JSON bytes."""
    cfg = {"bucket": "test-bucket", "region": "us-east-1",
           "endpoint_url": "http://minio:9000", "key_prefix": "p/"}
    await seed_default(db_session, storage=StorageSeed(config=cfg))
    await db_session.flush()

    sb = await db_session.get(StorageBackend, 1)
    assert sb is not None
    assert json.loads(bytes(sb.config_encrypted).decode("utf-8")) == cfg


@pytest.mark.slow
async def test_seed_demo_data_creates_pending_task(
    db_session: AsyncSession,
) -> None:
    """seed_demo_data adds 1 DownloadTask with status='pending' pointing to demo model."""
    await seed_demo_data(db_session)
    await db_session.flush()

    tasks = (await db_session.execute(select(DownloadTask))).scalars().all()
    assert len(tasks) == 1
    t = tasks[0]
    assert t.status == "pending"
    assert t.repo_id == "sentence-transformers/all-MiniLM-L6-v2"
    # M3 will pin a real SHA; M1 ships a placeholder revision string
    assert t.revision != ""
    assert t.tenant_id == 1
