"""Per-tenant quota (Phase 3 SP1; security §7).

Strong-consistent create check: SELECT ... FOR UPDATE the snapshot row +
a live COUNT(*) of non-terminal tasks. Only the `hard_block` action is
honored in SP1 (throttle/overage are Phase 4)."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.storage_object import StorageObject
from dlw.db.models.task import DownloadTask
from dlw.db.models.tenant import Tenant
from dlw.db.models.usage import QuotaSnapshot, UsageRecord

# Terminal statuses (codebase uses "succeeded", NOT "completed" —
# verified scheduler.py:156/200, tools/lint_invariants.py VALID_TASK_STATUS).
_TERMINAL = ("succeeded", "failed", "cancelled")


class QuotaExceeded(Exception):
    def __init__(self, metric: str) -> None:
        super().__init__(f"quota exceeded: {metric}")
        self.metric = metric


async def check_quota_for_new_task(
    session: AsyncSession, tenant_id: int
) -> None:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise QuotaExceeded("tenant_missing")
    # A tenant provisioned via /auth/callback after migrate-time has no
    # snapshot row until the minute aggregator runs. Without a row,
    # with_for_update() locks nothing and concurrent creates race past the
    # quota. Insert-then-lock guarantees exactly one lockable row (spec §3.7).
    await session.execute(
        pg_insert(QuotaSnapshot).values(tenant_id=tenant_id)
        .on_conflict_do_nothing(index_elements=["tenant_id"]))
    snap = (await session.execute(
        select(QuotaSnapshot)
        .where(QuotaSnapshot.tenant_id == tenant_id)
        .with_for_update()
    )).scalar_one()
    bytes_used = snap.bytes_used_month
    if tenant.quota_bytes_month and bytes_used >= tenant.quota_bytes_month:
        raise QuotaExceeded("bytes_month")
    live_concurrent = await session.scalar(
        select(func.count()).select_from(DownloadTask).where(
            DownloadTask.tenant_id == tenant_id,
            DownloadTask.status.not_in(_TERMINAL))) or 0
    if tenant.quota_concurrent and live_concurrent >= tenant.quota_concurrent:
        raise QuotaExceeded("concurrent_tasks")
    if tenant.quota_storage_gb and snap.storage_gb_used >= tenant.quota_storage_gb:
        raise QuotaExceeded("storage")
    # v2.1 SP1: tier-aware admission control. Even when below hard quota,
    # reject bulk tasks once the tenant is near its concurrent ceiling so
    # critical/standard work always has headroom. Uses sla_tier on the
    # tenant; default "standard" leaves the rejection threshold at >99%.
    # Disabled when quota_concurrent is 0 (unlimited) — there's no
    # meaningful "busy fraction" to compute.
    import os
    if (os.environ.get("DLW_SLA_TIER_ENABLED", "true").lower() in
            ("1", "true", "yes")
            and tenant.quota_concurrent):
        from dlw.services.sla_tier import admission_decision
        busy = live_concurrent / tenant.quota_concurrent
        if not admission_decision(tenant.sla_tier or "standard", busy):
            raise QuotaExceeded(f"admission_denied_{tenant.sla_tier or 'standard'}")


async def record_usage(
    session: AsyncSession, *, tenant_id: int, project_id: int | None,
    user_id: int | None, task_id: uuid.UUID | None, metric: str, value: int,
) -> None:
    session.add(UsageRecord(
        tenant_id=tenant_id, project_id=project_id, user_id=user_id,
        task_id=task_id, metric=metric, value=value))


async def aggregate_snapshots(session: AsyncSession) -> None:
    """Recompute every tenant's snapshot from usage_records (month window)
    + live concurrent count. Caller commits."""
    month_start = datetime.now(UTC).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0)
    tenant_ids = (await session.execute(select(Tenant.id))).scalars().all()
    for tid in tenant_ids:
        bytes_used = await session.scalar(
            select(func.coalesce(func.sum(UsageRecord.value), 0)).where(
                UsageRecord.tenant_id == tid,
                UsageRecord.metric == "bytes_month",
                UsageRecord.occurred_at >= month_start)) or 0
        concurrent = await session.scalar(
            select(func.count()).select_from(DownloadTask).where(
                DownloadTask.tenant_id == tid,
                DownloadTask.status.not_in(_TERMINAL))) or 0
        snap = await session.get(QuotaSnapshot, tid)
        if snap is None:
            snap = QuotaSnapshot(tenant_id=tid)
            session.add(snap)
        storage_bytes = await session.scalar(
            select(func.coalesce(func.sum(StorageObject.size), 0)).where(
                StorageObject.tenant_id == tid)) or 0
        snap.bytes_used_month = int(bytes_used)
        snap.concurrent_tasks = int(concurrent)
        snap.storage_gb_used = int(storage_bytes) // (1024 ** 3)
        snap.last_recomputed_at = datetime.now(UTC)
