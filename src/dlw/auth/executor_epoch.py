"""require_executor_epoch — Phase 2 W1 fence-token dependency.

Reads X-Executor-Epoch header + executor_id path param; looks up the executor
in DB; returns the row to the handler if epoch matches. 401 EPOCH_MISMATCH
otherwise.

Compose with require_bearer at the route level — both run; order doesn't
matter (different concerns).
"""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Path
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.api.tasks import _session
from dlw.db.models.executor import Executor


async def require_executor_epoch(
    executor_id: str = Path(..., description="Executor id from URL path"),
    # W6-D: accept Optional so dep body can raise 401 (not FastAPI auto-422) on missing
    x_executor_epoch: int | None = Header(default=None, alias="X-Executor-Epoch"),
    session: AsyncSession = Depends(_session),
) -> Executor:
    """Return the Executor row if header matches stored epoch; else 401/404."""
    if x_executor_epoch is None:
        raise HTTPException(
            status_code=401,
            detail="missing X-Executor-Epoch header",
        )
    ex = await session.get(Executor, executor_id)
    if ex is None:
        raise HTTPException(status_code=404, detail="executor not found")
    if ex.epoch != x_executor_epoch:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "EPOCH_MISMATCH",
                "expected": ex.epoch,
                "got": x_executor_epoch,
            },
        )
    return ex
