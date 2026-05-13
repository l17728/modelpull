"""Executor service: atomic register + heartbeat update.

Phase 2 W1: join_executor now bumps epoch atomically on every call.
First-time INSERT writes epoch=1; ON CONFLICT DO UPDATE bumps epoch+=1.
Status resets to 'joining' on every rejoin so that 'unhealthy'
(set by reclaim_stale_executors) flips back to 'joining' → 'healthy'
on the next heartbeat.
"""
from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.executor import Executor
from dlw.schemas.executor import ExecutorHeartbeat, ExecutorJoin


async def join_executor(session: AsyncSession, body: ExecutorJoin) -> Executor:
    """Atomic INSERT-or-bump. Returns the persisted Executor row with current epoch.

    PG INSERT ... ON CONFLICT (id) DO UPDATE is atomic for the bump — two
    concurrent join calls for the same id can never get the same epoch.
    """
    stmt = pg_insert(Executor).values(
        id=body.id,
        host_id=body.host_id,
        cert_fingerprint=body.cert_fingerprint,
        capabilities=body.capabilities,
        status="joining",
        epoch=1,
    ).on_conflict_do_update(
        index_elements=["id"],
        set_=dict(
            status="joining",
            host_id=body.host_id,
            cert_fingerprint=body.cert_fingerprint,
            capabilities=body.capabilities,
            epoch=Executor.__table__.c.epoch + 1,
        ),
    ).returning(Executor)
    row = (
        await session.execute(stmt, execution_options={"populate_existing": True})
    ).scalar_one()
    return row


async def record_heartbeat(
    session: AsyncSession,
    executor_id: str,
    body: ExecutorHeartbeat,
) -> Executor:
    """Update non-status fields + route status mutation through state machine.

    W2a §3.6: state transitions (joining → healthy, suspect → degraded) and
    counter resets are handled inside transition_executor. This function
    retains responsibility for the non-status fields posted in the heartbeat
    body (health_score, parts_dir_bytes).
    """
    from dlw.services.state_machine import transition_executor   # local import: avoids cycle

    ex = await session.get(Executor, executor_id)
    if ex is None:
        raise LookupError(f"executor {executor_id} not found (must POST /join first)")
    ex.health_score = body.health_score
    ex.parts_dir_bytes = body.parts_dir_bytes
    await transition_executor(
        session, ex,
        event="heartbeat_ok",
        reason="hb_received",
        metadata={"health_score": body.health_score},
    )
    return ex
