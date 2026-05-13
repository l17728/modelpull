"""Tests for executor state machine (Phase 2 W2a §6 rules)."""
from __future__ import annotations

import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.base import Base
from dlw.db.models.executor import Executor
from dlw.db.models.executor_status_history import ExecutorStatusHistory
from dlw.services.state_machine import (
    DEGRADED_RECOVER_OK,
    DEGRADED_STREAK_FAULTY,
    HB_TIMEOUT_TO_FAULTY,
    HB_TIMEOUT_TO_SUSPECT,
    TASK_FAIL_TO_DEGRADED,
    Transition,
    transition_executor,
)


@pytest.fixture(scope="module", autouse=True)
async def _create_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _make_executor(
    session: AsyncSession, *, ex_id: str = "ex-1", host: str = "host-A",
    status: str = "healthy", epoch: int = 1,
) -> Executor:
    ex = Executor(
        id=ex_id, host_id=host, cert_fingerprint="x",
        status=status, epoch=epoch,
    )
    session.add(ex)
    await session.flush()
    return ex


@pytest.mark.slow
async def test_healthy_to_degraded_after_3_task_failures(db_session: AsyncSession) -> None:
    ex = await _make_executor(db_session, ex_id="ex-h2d")
    # First two failures: counter rises, no transition.
    for _ in range(TASK_FAIL_TO_DEGRADED - 1):
        t = await transition_executor(db_session, ex, event="task_failure", reason="t")
        assert t is None
    # Third failure: → degraded.
    t = await transition_executor(db_session, ex, event="task_failure", reason="t")
    assert t == Transition(
        from_status="healthy", to_status="degraded",
        reason="3_task_failures", event="task_failure",
    )
    assert ex.status == "degraded"
    assert ex.degraded_failure_streak == 0    # Reset on entry to degraded.


@pytest.mark.slow
async def test_degraded_to_faulty_after_streak_10(db_session: AsyncSession) -> None:
    ex = await _make_executor(db_session, ex_id="ex-d2f", status="degraded")
    for _ in range(DEGRADED_STREAK_FAULTY - 1):
        t = await transition_executor(db_session, ex, event="task_failure", reason="t")
        assert t is None
    t = await transition_executor(db_session, ex, event="task_failure", reason="t")
    assert t.from_status == "degraded" and t.to_status == "faulty"
    assert ex.status == "faulty"


@pytest.mark.slow
async def test_degraded_recovers_to_healthy_after_5_ok(db_session: AsyncSession) -> None:
    ex = await _make_executor(
        db_session, ex_id="ex-d2h", status="degraded",
    )
    ex.degraded_failure_streak = 4   # Should be reset on entry to healthy.
    for _ in range(DEGRADED_RECOVER_OK - 1):
        t = await transition_executor(db_session, ex, event="task_success", reason="t")
        assert t is None
    t = await transition_executor(db_session, ex, event="task_success", reason="t")
    assert t.to_status == "healthy"
    assert ex.status == "healthy"
    assert ex.degraded_failure_streak == 0


@pytest.mark.slow
async def test_suspect_to_degraded_on_heartbeat_ok(db_session: AsyncSession) -> None:
    ex = await _make_executor(db_session, ex_id="ex-s2d", status="suspect")
    t = await transition_executor(db_session, ex, event="heartbeat_ok", reason="hb")
    assert t.from_status == "suspect" and t.to_status == "degraded"
    assert ex.consecutive_heartbeat_failures == 0


@pytest.mark.slow
async def test_transition_writes_history_row(db_session: AsyncSession) -> None:
    ex = await _make_executor(db_session, ex_id="ex-hist")
    await transition_executor(db_session, ex, event="task_failure", reason="t")
    await transition_executor(db_session, ex, event="task_failure", reason="t")
    await transition_executor(db_session, ex, event="task_failure", reason="t")
    await db_session.flush()
    n = await db_session.scalar(
        select(func.count()).select_from(ExecutorStatusHistory)
        .where(ExecutorStatusHistory.executor_id == "ex-hist")
    )
    assert n == 1   # Only the threshold-crossing event writes a row.
    row = (await db_session.execute(
        select(ExecutorStatusHistory)
        .where(ExecutorStatusHistory.executor_id == "ex-hist")
    )).scalar_one()
    assert row.from_status == "healthy"
    assert row.to_status == "degraded"
    assert row.event == "task_failure"


@pytest.mark.slow
async def test_no_transition_returns_none_no_history(db_session: AsyncSession) -> None:
    ex = await _make_executor(db_session, ex_id="ex-noop")
    t = await transition_executor(db_session, ex, event="task_failure", reason="t")
    assert t is None
    await db_session.flush()
    n = await db_session.scalar(
        select(func.count()).select_from(ExecutorStatusHistory)
        .where(ExecutorStatusHistory.executor_id == "ex-noop")
    )
    assert n == 0
