"""Quota service: strong-consistent check + record + aggregate (SP1)."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.db.base import Base
from dlw.services.quota import (
    QuotaExceeded,
    aggregate_snapshots,
    check_quota_for_new_task,
    record_usage,
)

pytestmark = pytest.mark.slow


@pytest.fixture
async def seeded(engine):
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Project, Tenant, User
    from dlw.db.models.usage import QuotaSnapshot
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(Tenant(id=1, slug="t", display_name="T",
                     quota_bytes_month=1000, quota_concurrent=2))
        await s.flush()
        s.add_all([Project(id=1, tenant_id=1, name="d"),
                   User(id=1, tenant_id=1, oidc_subject="u",
                        email="u@t", role="tenant_operator"),
                   StorageBackend(id=1, tenant_id=1, name="d",
                                  backend_type="s3", config_encrypted=b""),
                   QuotaSnapshot(tenant_id=1)])
        await s.commit()
    yield factory
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def test_under_limit_passes(seeded):
    async with seeded() as s:
        await check_quota_for_new_task(s, 1)  # no raise


async def test_snapshotless_tenant_is_lockable(seeded):
    from sqlalchemy import select

    from dlw.db.models.tenant import Tenant
    from dlw.db.models.usage import QuotaSnapshot
    async with seeded() as s:
        s.add(Tenant(id=2, slug="t2", display_name="T2",
                      quota_bytes_month=1000, quota_concurrent=2))
        await s.commit()
        await check_quota_for_new_task(s, 2)
        await s.commit()
        row = (await s.execute(
            select(QuotaSnapshot).where(QuotaSnapshot.tenant_id == 2))
        ).scalar_one()
        assert row.bytes_used_month == 0


async def test_concurrent_limit_blocks(seeded):
    from dlw.db.models.task import DownloadTask
    async with seeded() as s:
        for i in range(2):
            s.add(DownloadTask(tenant_id=1, project_id=1, owner_user_id=1,
                               repo_id=f"o/r{i}", revision="0" * 40,
                               storage_id=1, path_template="{repo_id}",
                               status="pending"))
        await s.commit()
        with pytest.raises(QuotaExceeded) as e:
            await check_quota_for_new_task(s, 1)
        assert e.value.metric == "concurrent_tasks"


async def test_bytes_limit_blocks(seeded):
    from dlw.db.models.usage import QuotaSnapshot
    async with seeded() as s:
        snap = (await s.execute(
            select(QuotaSnapshot).where(QuotaSnapshot.tenant_id == 1))
        ).scalar_one()
        snap.bytes_used_month = 1000
        await s.commit()
        with pytest.raises(QuotaExceeded) as e:
            await check_quota_for_new_task(s, 1)
        assert e.value.metric == "bytes_month"


async def test_record_usage_appends(seeded):
    from dlw.db.models.usage import UsageRecord
    async with seeded() as s:
        await record_usage(s, tenant_id=1, project_id=1, user_id=1,
                           task_id=uuid.uuid4(), metric="bytes_month",
                           value=500)
        await s.commit()
        rows = (await s.execute(select(UsageRecord))).scalars().all()
        assert len(rows) == 1 and rows[0].value == 500


async def test_aggregate_recomputes_snapshot(seeded):
    from dlw.db.models.usage import QuotaSnapshot
    async with seeded() as s:
        await record_usage(s, tenant_id=1, project_id=1, user_id=1,
                           task_id=uuid.uuid4(), metric="bytes_month",
                           value=300)
        await s.commit()
        await aggregate_snapshots(s)
        await s.commit()
        snap = (await s.execute(
            select(QuotaSnapshot).where(QuotaSnapshot.tenant_id == 1))
        ).scalar_one()
        assert snap.bytes_used_month == 300
