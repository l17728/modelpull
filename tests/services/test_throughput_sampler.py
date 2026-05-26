"""v2.1 Sprint 7 — throughput sampler unit + integration tests."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.db.base import Base
from dlw.db.models.chunk_throughput import ChunkThroughputSample
from dlw.db.models.tenant import Tenant
from dlw.services.throughput_sampler import (
    Sample,
    _buffer_for_tests,
    _reset_for_tests,
    flush_once,
    record_sample,
    retention_tick,
)


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    import dlw.db.models  # noqa: F401
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add(Tenant(id=900, slug="tsamp", display_name="TSamp"))
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
async def _per_test_cleanup(engine):
    _reset_for_tests()
    yield
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        from sqlalchemy import delete
        await s.execute(delete(ChunkThroughputSample))
        await s.commit()


@pytest.fixture
def factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


def _sample(**overrides) -> Sample:
    defaults = dict(
        executor_id="ex-1", source_id="huggingface", file_type="safetensors",
        bytes_transferred=1024 * 1024 * 100, duration_ms=1234,
        tenant_id=900)
    defaults.update(overrides)
    return Sample(**defaults)


# ---------------------------------------------------------------------------
# record_sample: hot path semantics

def test_record_sample_appends_to_buffer():
    record_sample(_sample())
    assert len(_buffer_for_tests()) == 1


def test_record_sample_drops_malformed_zero_duration():
    """duration_ms=0 would violate the CHECK constraint; drop before buffer."""
    record_sample(_sample(duration_ms=0))
    assert len(_buffer_for_tests()) == 0


def test_record_sample_drops_negative_bytes():
    record_sample(_sample(bytes_transferred=-1))
    assert len(_buffer_for_tests()) == 0


def test_record_sample_buffer_bounded():
    """Beyond MAX_BUFFER_SIZE, oldest samples are silently dropped — better
    than OOM under sustained DB outage."""
    from dlw.services.throughput_sampler import MAX_BUFFER_SIZE
    for i in range(MAX_BUFFER_SIZE + 50):
        record_sample(_sample(executor_id=f"ex-{i}"))
    assert len(_buffer_for_tests()) == MAX_BUFFER_SIZE


# ---------------------------------------------------------------------------
# flush_once: drains + bulk-inserts

async def test_flush_once_empty_returns_zero(factory):
    n = await flush_once(factory)
    assert n == 0


async def test_flush_once_persists_and_drains(factory):
    for i in range(3):
        record_sample(_sample(executor_id=f"ex-{i}"))
    assert len(_buffer_for_tests()) == 3
    n = await flush_once(factory)
    assert n == 3
    assert len(_buffer_for_tests()) == 0  # drained
    async with factory() as s:
        rows = (await s.execute(select(ChunkThroughputSample))).scalars().all()
    assert len(rows) == 3
    assert {r.executor_id for r in rows} == {"ex-0", "ex-1", "ex-2"}


async def test_flush_once_idempotent_when_buffer_re_filled(factory):
    """Calling flush_once twice with new samples between calls should
    persist all of them (i.e. drain doesn't lose samples added during
    the second flush)."""
    record_sample(_sample(executor_id="ex-A"))
    n1 = await flush_once(factory)
    record_sample(_sample(executor_id="ex-B"))
    n2 = await flush_once(factory)
    assert (n1, n2) == (1, 1)
    async with factory() as s:
        rows = (await s.execute(select(ChunkThroughputSample))).scalars().all()
    assert {r.executor_id for r in rows} == {"ex-A", "ex-B"}


# ---------------------------------------------------------------------------
# retention_tick: deletes rows older than cutoff

async def test_retention_tick_deletes_old_rows(factory):
    # Seed: 2 old rows + 1 fresh row
    old_time = datetime.now(UTC) - timedelta(days=10)
    async with factory() as s:
        s.add_all([
            ChunkThroughputSample(
                executor_id="old-1", source_id="hf", file_type="bin",
                bytes_transferred=100, duration_ms=500,
                tenant_id=900, recorded_at=old_time),
            ChunkThroughputSample(
                executor_id="old-2", source_id="hf", file_type="bin",
                bytes_transferred=200, duration_ms=600,
                tenant_id=900, recorded_at=old_time),
            ChunkThroughputSample(
                executor_id="fresh", source_id="hf", file_type="bin",
                bytes_transferred=300, duration_ms=700, tenant_id=900),
        ])
        await s.commit()

    deleted = await retention_tick(factory, retention_days=7)
    assert deleted == 2

    async with factory() as s:
        rows = (await s.execute(select(ChunkThroughputSample))).scalars().all()
    assert len(rows) == 1
    assert rows[0].executor_id == "fresh"


async def test_retention_tick_zero_days_is_noop(factory):
    record_sample(_sample())
    await flush_once(factory)
    deleted = await retention_tick(factory, retention_days=0)
    assert deleted == 0
    async with factory() as s:
        rows = (await s.execute(select(ChunkThroughputSample))).scalars().all()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# flush_loop: cancellation + final-flush

async def test_flush_loop_drains_then_cancels(factory):
    from dlw.services.throughput_sampler import flush_loop
    record_sample(_sample(executor_id="cancel-target"))
    task = asyncio.create_task(flush_loop(factory, interval_seconds=0.05))
    # Wait for at least one tick to drain
    await asyncio.sleep(0.2)
    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=1)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass
    async with factory() as s:
        rows = (await s.execute(select(ChunkThroughputSample))).scalars().all()
    assert len(rows) == 1
    assert rows[0].executor_id == "cancel-target"


async def test_flush_loop_final_flush_on_cancel(factory):
    """A sample arriving JUST before cancel must still land — the loop's
    CancelledError handler does a final flush_once."""
    from dlw.services.throughput_sampler import flush_loop
    # Use a long interval so the loop is napping when we cancel
    task = asyncio.create_task(flush_loop(factory, interval_seconds=30))
    await asyncio.sleep(0.05)  # let the loop reach sleep
    record_sample(_sample(executor_id="final-flush-target"))
    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=2)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass
    async with factory() as s:
        rows = (await s.execute(select(ChunkThroughputSample))).scalars().all()
    assert len(rows) == 1
    assert rows[0].executor_id == "final-flush-target"
