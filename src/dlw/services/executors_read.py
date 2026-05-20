"""UI-SP3 executors list (read-only, tenant-scoped or admin-wide)."""
from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.auth.principal import Principal
from dlw.db.models.executor import Executor

_VALID_STATUS = {"joining", "healthy", "degraded", "suspect", "faulty"}


def _is_admin(principal: Principal) -> bool:
    return (getattr(principal, "is_service", False)
            or principal.role == "system_admin")


async def list_executors_for_principal(
    session: AsyncSession, principal: Principal,
    status_filter: str | None,
) -> list[Executor]:
    stmt = select(Executor)
    if not _is_admin(principal):
        stmt = stmt.where(or_(
            Executor.tenant_id.is_(None),
            Executor.tenant_id == principal.tenant_id))
    if status_filter:
        if status_filter not in _VALID_STATUS:
            return []
        stmt = stmt.where(Executor.status == status_filter)
    stmt = stmt.order_by(Executor.host_id.asc().nullslast(),
                         Executor.id.asc())
    return list((await session.execute(stmt)).scalars().all())
