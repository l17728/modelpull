"""GET /api/v1/executors — browser-facing executor list (UI-SP3).

NOT in src/dlw/api/executors.py because that file is mTLS-only per
tools/lint_invariants.py:check_no_bearer_on_executor_routes (it forbids
require_bearer-style auth there). This module uses require_perm.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dlw.auth.principal import Principal
from dlw.authz.deps import require_perm
from dlw.db.session import get_engine
from dlw.schemas.executor_read import ExecutorListResponse, ExecutorRead
from dlw.services.executors_read import list_executors_for_principal

router = APIRouter(prefix="/api/v1/executors", tags=["executors"])

_StatusLit = Literal["joining", "healthy", "degraded", "suspect", "faulty"]


async def _session():
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with factory() as session:
        yield session


@router.get("")
async def list_executors(
    status: _StatusLit | None = Query(default=None),
    principal: Principal = Depends(require_perm("/api/v1/executors*", "GET")),
    session: AsyncSession = Depends(_session),
) -> ExecutorListResponse:
    rows = await list_executors_for_principal(session, principal, status)
    return ExecutorListResponse(
        items=[ExecutorRead.model_validate(r) for r in rows])
