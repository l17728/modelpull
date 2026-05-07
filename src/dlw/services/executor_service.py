"""Executor service: join (idempotent register) + heartbeat (upsert state)."""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.executor import Executor
from dlw.schemas.executor import ExecutorHeartbeat, ExecutorJoin


async def join_executor(session: AsyncSession, body: ExecutorJoin) -> Executor:
    """Idempotent: if executor with this id exists, return it (no schema change).

    Phase 2 adds executor_epoch increment on rejoin (fence-token invariant).
    """
    existing = await session.get(Executor, body.id)
    if existing is not None:
        return existing
    ex = Executor(
        id=body.id,
        host_id=body.host_id,
        cert_fingerprint=body.cert_fingerprint,
        capabilities=body.capabilities,
        status="joining",
    )
    session.add(ex)
    await session.flush()
    return ex


async def record_heartbeat(
    session: AsyncSession,
    executor_id: str,
    body: ExecutorHeartbeat,
) -> Executor:
    """Update last_heartbeat_at + health_score + parts_dir_bytes.

    Transitions joining → healthy on first heartbeat.
    """
    ex = await session.get(Executor, executor_id)
    if ex is None:
        raise LookupError(f"executor {executor_id} not found (must POST /join first)")
    ex.last_heartbeat_at = datetime.now(UTC)
    ex.health_score = body.health_score
    ex.parts_dir_bytes = body.parts_dir_bytes
    if ex.status == "joining":
        ex.status = "healthy"
    ex.consecutive_heartbeat_failures = 0
    return ex
