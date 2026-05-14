"""Tests for executor_service: upsert_executor_with_cert + heartbeat upsert (W3a)."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.base import Base
from dlw.db.models.executor import Executor
from dlw.schemas.executor import ExecutorHeartbeat
from dlw.services.executor_service import record_heartbeat, upsert_executor_with_cert


_FP = "SHA256:" + "ab" * 32
_SEED = b"\x07" * 32


def _upsert_kwargs(executor_id: str, host_id: str, *, fp: str = _FP):
    return dict(
        executor_id=executor_id, host_id=host_id, capabilities={},
        cert_fingerprint=fp, hmac_seed=_SEED,
    )


@pytest.fixture(scope="module", autouse=True)
async def _create_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.mark.slow
async def test_upsert_creates_executor_with_cert_and_seed(db_session: AsyncSession) -> None:
    ex = await upsert_executor_with_cert(
        db_session,
        **_upsert_kwargs("host-a-w1", "host-a"),
    )
    await db_session.commit()
    assert ex.id == "host-a-w1"
    assert ex.status == "joining"
    assert ex.health_score == 100
    assert ex.cert_fingerprint == _FP
    assert bytes(ex.hmac_seed_encrypted) == _SEED


@pytest.mark.slow
async def test_upsert_idempotent(db_session: AsyncSession) -> None:
    await upsert_executor_with_cert(db_session, **_upsert_kwargs("host-b-w1", "host-b"))
    await db_session.commit()
    again = await upsert_executor_with_cert(
        db_session, **_upsert_kwargs("host-b-w1", "host-b"),
    )
    await db_session.commit()
    assert again.id == "host-b-w1"


@pytest.mark.slow
async def test_heartbeat_updates_health_and_timestamp(db_session: AsyncSession) -> None:
    await upsert_executor_with_cert(db_session, **_upsert_kwargs("host-c-w1", "host-c"))
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


@pytest.fixture(scope="module")
async def env():
    """No-op: _create_tables autouse fixture ensures tables exist."""


@pytest.mark.slow
async def test_upsert_first_time_returns_epoch_1(db_session: AsyncSession, env) -> None:
    """First upsert for a brand new executor_id assigns epoch=1."""
    ex = await upsert_executor_with_cert(
        db_session, **_upsert_kwargs("new-host-worker-1", "new-host"),
    )
    assert ex.epoch == 1


@pytest.mark.slow
async def test_upsert_existing_executor_increments_epoch(
    db_session: AsyncSession, env,
) -> None:
    """Repeated upsert for same id bumps epoch atomically: 1 → 2 → 3."""
    kw = _upsert_kwargs("bump-host-worker-1", "bump-host")
    ex1 = await upsert_executor_with_cert(db_session, **kw); await db_session.commit()
    assert ex1.epoch == 1
    ex2 = await upsert_executor_with_cert(db_session, **kw); await db_session.commit()
    assert ex2.epoch == 2
    ex3 = await upsert_executor_with_cert(db_session, **kw); await db_session.commit()
    assert ex3.epoch == 3


@pytest.mark.slow
async def test_upsert_concurrent_returns_distinct_epochs(engine, env) -> None:
    """asyncio.gather × 2 upsert calls for the same id must yield distinct epochs."""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    factory = async_sessionmaker(engine, expire_on_commit=False)
    kw = _upsert_kwargs("race-host-worker-1", "race-host")

    async def upsert_in_own_session():
        async with factory() as s:
            ex = await upsert_executor_with_cert(s, **kw)
            await s.commit()
            return ex.epoch

    epochs = await asyncio.gather(upsert_in_own_session(), upsert_in_own_session())
    assert sorted(epochs) == [1, 2]  # PG atomic; one wins INSERT, the other UPDATE
