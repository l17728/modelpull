"""Executor service: atomic register + heartbeat update.

Phase 2 W1: join_executor now bumps epoch atomically on every call.
First-time INSERT writes epoch=1; ON CONFLICT DO UPDATE bumps epoch+=1.
Status resets to 'joining' on every rejoin so that 'unhealthy'
(set by reclaim_stale_executors) flips back to 'joining' → 'healthy'
on the next heartbeat.

Phase 2 W3a: join_executor renamed to upsert_executor_with_cert; now accepts
cert_fingerprint + hmac_seed as explicit kwargs rather than from ExecutorJoin schema.
"""
from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.executor import Executor
from dlw.schemas.executor import ExecutorHeartbeat


async def upsert_executor_with_cert(
    session: AsyncSession,
    *,
    executor_id: str,
    host_id: str,
    capabilities: dict,
    cert_fingerprint: str,
    hmac_seed: bytes,
) -> Executor:
    """W3a: INSERT-or-bump executor row, writing cert_fingerprint +
    hmac_seed_encrypted. Same atomic epoch semantics as W1 join_executor:
    epoch=1 on insert, +1 on conflict; status='joining'. Caller commits."""
    stmt = pg_insert(Executor).values(
        id=executor_id,
        host_id=host_id,
        cert_fingerprint=cert_fingerprint,
        hmac_seed_encrypted=hmac_seed,
        capabilities=capabilities,
        status="joining",
        epoch=1,
    ).on_conflict_do_update(
        index_elements=["id"],
        set_=dict(
            status="joining",
            host_id=host_id,
            cert_fingerprint=cert_fingerprint,
            hmac_seed_encrypted=hmac_seed,
            capabilities=capabilities,
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
    if body.disk_free_gb is not None:
        ex.disk_free_gb = body.disk_free_gb
    await transition_executor(
        session, ex,
        event="heartbeat_ok",
        reason="hb_received",
        metadata={"health_score": body.health_score},
    )
    return ex
