"""Integration tests for SLA tier × scheduler + quota.

Verifies that v2.1 SP1's tier weights actually change scheduling order
and that admission_control rejects bulk on a busy tenant."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.db.base import Base


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    import dlw.db.models  # noqa: F401
    from dlw.db.models.executor import Executor
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.task import DownloadTask, FileSubTask
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        # Three tenants, one per tier
        s.add(Tenant(id=201, slug="crit", display_name="Critical",
                     sla_tier="critical", quota_concurrent=10))
        s.add(Tenant(id=202, slug="std", display_name="Standard",
                     sla_tier="standard", quota_concurrent=10))
        s.add(Tenant(id=203, slug="bulk", display_name="Bulk",
                     sla_tier="bulk", quota_concurrent=10))
        await s.flush()
        for tid in (201, 202, 203):
            s.add(Project(id=tid, tenant_id=tid, name="d"))
            s.add(User(id=tid, tenant_id=tid, oidc_subject=f"u{tid}",
                       role="tenant_operator"))
        s.add(StorageBackend(id=201, tenant_id=201, name="s201",
                              backend_type="s3", config_encrypted=b""))
        s.add(StorageBackend(id=202, tenant_id=202, name="s202",
                              backend_type="s3", config_encrypted=b""))
        s.add(StorageBackend(id=203, tenant_id=203, name="s203",
                              backend_type="s3", config_encrypted=b""))
        await s.flush()
        # A healthy executor for the scheduler to claim against
        s.add(Executor(
            id="sched-exec-1", host_id="hostA", capabilities={},
            cert_fingerprint="aa" * 32,
            status="healthy", disk_free_gb=100, parts_dir_bytes=0,
            epoch=1, last_heartbeat_at=None))
        await s.flush()
        # One task per tenant; same priority so tier is the differentiator
        tasks = {}
        for tid in (201, 202, 203):
            t = DownloadTask(
                id=uuid.uuid4(), tenant_id=tid, project_id=tid,
                owner_user_id=tid, storage_id=tid,
                repo_id=f"org/m{tid}", revision="0" * 40,
                path_template="hf/{model}/{revision}/{file}",
                status="downloading", priority=1,
                source_strategy="auto_balance", source_blacklist=[])
            s.add(t)
            tasks[tid] = t
        await s.flush()
        # Subtasks: one pending per task, ALL with the same created_at offset
        # so the tier ordering is the only differentiator. We insert bulk
        # FIRST (oldest created_at) so the v2.0 created_at-only ordering
        # would pick bulk; with SLA enabled, critical should win.
        for tid in (203, 202, 201):
            s.add(FileSubTask(
                id=uuid.uuid4(), task_id=tasks[tid].id, tenant_id=tid,
                filename=f"f{tid}.bin",
                file_size=1024, expected_sha256="0" * 64,
                status="pending"))
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def session(engine):
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        yield s


async def test_scheduler_picks_critical_first_when_sla_enabled(
    session, monkeypatch,
):
    """With SLA enabled, the scheduler should claim the critical tenant's
    subtask before bulk — even though bulk was inserted first (older
    created_at)."""
    monkeypatch.setenv("DLW_SLA_TIER_ENABLED", "true")
    # Re-import scheduler so the module-level flag re-reads env. The flag
    # is captured at import time, so we override the module attribute too.
    import dlw.services.scheduler as sched
    monkeypatch.setattr(sched, "_SLA_TIER_ENABLED", True)

    sub, _ = await sched.claim_one_subtask(session, "sched-exec-1", 1)
    assert sub is not None
    # Look up the tenant of the claimed task
    from dlw.db.models.task import DownloadTask
    task = await session.get(DownloadTask, sub.task_id)
    assert task.tenant_id == 201, \
        f"expected critical tenant 201, got {task.tenant_id}"
    await session.rollback()


async def test_admission_denied_for_bulk_when_busy(session, monkeypatch):
    """Fill the bulk tenant to 90%+ concurrent usage; new task gets
    admission_denied_bulk."""
    from dlw.db.models.task import DownloadTask
    from dlw.services.quota import QuotaExceeded, check_quota_for_new_task

    monkeypatch.setenv("DLW_SLA_TIER_ENABLED", "true")
    # Bootstrap already inserted 1 task for tenant 203 → 8 more = 9 total.
    # quota_concurrent=10 so busy = 9/10 = 0.9, which hits the bulk
    # admission threshold (> 0.90 rejects bulk) but doesn't hit the hard
    # quota_concurrent limit. We want admission_denied, not concurrent_tasks.
    for i in range(8):
        session.add(DownloadTask(
            id=uuid.uuid4(), tenant_id=203, project_id=203,
            owner_user_id=203, storage_id=203,
            repo_id=f"org/fill{i}", revision="0" * 40,
            path_template="hf/{model}/{revision}/{file}",
            status="downloading", priority=1,
            source_strategy="auto_balance", source_blacklist=[]))
    await session.flush()
    with pytest.raises(QuotaExceeded) as exc:
        await check_quota_for_new_task(session, 203)
    assert "admission_denied" in exc.value.metric
    await session.rollback()


async def test_admission_critical_passes_even_when_busy(session, monkeypatch):
    """Critical tenant gets admitted even at >99% busy."""
    from dlw.db.models.task import DownloadTask
    from dlw.services.quota import check_quota_for_new_task

    monkeypatch.setenv("DLW_SLA_TIER_ENABLED", "true")
    # Bootstrap already inserted 1 task for tenant 201. Add 8 more → 9 total,
    # busy = 0.9. Critical's admission_decision is True at any fraction, so
    # the call should not raise admission_denied. Hard quota_concurrent=10
    # is not yet hit (9 < 10).
    for i in range(8):
        session.add(DownloadTask(
            id=uuid.uuid4(), tenant_id=201, project_id=201,
            owner_user_id=201, storage_id=201,
            repo_id=f"org/cfill{i}", revision="0" * 40,
            path_template="hf/{model}/{revision}/{file}",
            status="downloading", priority=1,
            source_strategy="auto_balance", source_blacklist=[]))
    await session.flush()
    # No exception expected
    await check_quota_for_new_task(session, 201)
    await session.rollback()
