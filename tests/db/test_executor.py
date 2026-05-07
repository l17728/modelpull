from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.base import Base
from dlw.db.models.executor import Executor


@pytest.fixture(scope="module", autouse=True)
async def _create_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.mark.slow
async def test_executor_create(db_session: AsyncSession) -> None:
    executor = Executor(
        id="host-12.local-worker-1",
        host_id="host-12.local",
        cert_fingerprint="placeholder-phase1",
        status="joining",
    )
    db_session.add(executor)
    await db_session.commit()
    assert executor.health_score == 100  # default
    assert executor.consecutive_heartbeat_failures == 0


@pytest.mark.slow
async def test_executor_capabilities_jsonb(db_session: AsyncSession) -> None:
    executor = Executor(
        id="host-13.local-worker-1",
        host_id="host-13.local",
        cert_fingerprint="placeholder-phase1",
        status="healthy",
        capabilities={"nic_speed_gbps": 10, "regions": ["cn-north-1"]},
    )
    db_session.add(executor)
    await db_session.commit()
    await db_session.refresh(executor)
    assert executor.capabilities["nic_speed_gbps"] == 10
    assert "cn-north-1" in executor.capabilities["regions"]
