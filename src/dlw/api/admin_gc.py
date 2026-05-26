"""v2.1 — Admin REST endpoints for the Physical GC.

system_admin only. Lets an operator manually trigger a GC pass and
inspect the last-run summary without waiting for the cron loop."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dlw.auth.principal import Principal, require_principal
from dlw.db.session import get_engine
from dlw.services.physical_gc import (
    DefaultObjectStoreDeleter,
    PhysicalGCDisabled,
    gc_run_once,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/gc", tags=["admin-gc"])


# In-memory last-run summary so /status can show what the last pass did
# without re-reading audit_log. Lost on restart — that's fine; the
# audit row is the durable record.
_LAST_RUN: dict[str, Any] = {"never_ran": True}
_LAST_RUN_LOCK = asyncio.Lock()


async def _session() -> AsyncSession:
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with factory() as s:
        yield s


def _factory_for_deleter():
    return async_sessionmaker(get_engine(), expire_on_commit=False)


@router.post("/run")
async def post_gc_run(
    body: dict | None = None,
    principal: Principal = Depends(require_principal),
    session: AsyncSession = Depends(_session),
) -> dict:
    """Manually trigger one GC pass. body may include {"tenant_id": int}
    to restrict to a single tenant; omitted means tombstone-cleanup
    across all tenants."""
    if principal.role != "system_admin":
        logger.warning(
            "admin_gc.role_denied user_id=%s role=%s",
            principal.user_id, principal.role)
        raise HTTPException(status_code=403, detail="system_admin role required")
    tenant_id = (body or {}).get("tenant_id")
    deleter = DefaultObjectStoreDeleter(_factory_for_deleter())
    try:
        summary = await gc_run_once(
            session, deleter=deleter, tenant_id=tenant_id,
            actor_user_id=principal.user_id)
    except PhysicalGCDisabled as e:
        raise HTTPException(
            status_code=503,
            detail={"code": "GC_DISABLED",
                    "message": "set DLW_PHYSICAL_GC_ENABLED=true to enable"}) from e
    await session.commit()
    async with _LAST_RUN_LOCK:
        _LAST_RUN.clear()
        _LAST_RUN["ran_at"] = datetime.utcnow().isoformat() + "Z"
        _LAST_RUN["triggered_by"] = principal.user_id
        _LAST_RUN["tenant_id"] = tenant_id
        _LAST_RUN.update(summary)
    return summary


@router.get("/status")
async def get_gc_status(
    principal: Principal = Depends(require_principal),
) -> dict:
    """Return the in-memory last-run summary. Always reachable (no DB
    query) so it's safe to call from monitoring even when the DB is
    under load."""
    if principal.role != "system_admin":
        logger.warning(
            "admin_gc.role_denied user_id=%s role=%s",
            principal.user_id, principal.role)
        raise HTTPException(status_code=403, detail="system_admin role required")
    async with _LAST_RUN_LOCK:
        return dict(_LAST_RUN)
