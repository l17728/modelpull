"""Per-tenant quota (Phase 3 SP1).

NOTE: this is the Task-9 temporary stub. Task 11 replaces the body with the
real strong-consistent check + usage recording + minute aggregation. The
public surface (`QuotaExceeded`, `check_quota_for_new_task`) is stable."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession


class QuotaExceeded(Exception):
    def __init__(self, metric: str) -> None:
        super().__init__(f"quota exceeded: {metric}")
        self.metric = metric


async def check_quota_for_new_task(
    session: AsyncSession, tenant_id: int
) -> None:
    return None
