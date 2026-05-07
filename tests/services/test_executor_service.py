"""Tests for executor_service: join + heartbeat upsert."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.base import Base
from dlw.db.models.executor import Executor
from dlw.schemas.executor import ExecutorHeartbeat, ExecutorJoin
from dlw.services.executor_service import join_executor, record_heartbeat


@pytest.fixture(scope="module", autouse=True)
async def _create_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.mark.slow
async def test_join_creates_executor(db_session: AsyncSession) -> None:
    body = ExecutorJoin(
        id="host-a-w1", host_id="host-a", capabilities={"nic_speed_gbps": 10},
    )
    ex = await join_executor(db_session, body)
    await db_session.commit()
    assert ex.id == "host-a-w1"
    assert ex.status == "joining"
    assert ex.health_score == 100


@pytest.mark.slow
async def test_join_idempotent(db_session: AsyncSession) -> None:
    body = ExecutorJoin(id="host-b-w1", host_id="host-b")
    await join_executor(db_session, body)
    await db_session.commit()
    again = await join_executor(db_session, body)
    await db_session.commit()
    assert again.id == "host-b-w1"


@pytest.mark.slow
async def test_heartbeat_updates_health_and_timestamp(db_session: AsyncSession) -> None:
    await join_executor(db_session, ExecutorJoin(id="host-c-w1", host_id="host-c"))
    await db_session.commit()
    before = datetime.now(UTC)
    ex = await record_heartbeat(
        db_session, "host-c-w1",
        ExecutorHeartbeat(health_score=87, parts_dir_bytes=1024),
    )
    await db_session.commit()
    assert ex.status == "healthy"
    assert ex.health_score == 87
    assert ex.parts_dir_bytes == 1024
    assert ex.last_heartbeat_at is not None
    assert ex.last_heartbeat_at >= before - timedelta(seconds=1)


@pytest.mark.slow
async def test_heartbeat_unknown_executor_raises(db_session: AsyncSession) -> None:
    with pytest.raises(LookupError):
        await record_heartbeat(
            db_session, "no-such-executor",
            ExecutorHeartbeat(),
        )
