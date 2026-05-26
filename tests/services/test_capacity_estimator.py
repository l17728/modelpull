"""v2.1 Sprint 8 — Capacity estimator (reads chunk_throughput_sample)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.db.base import Base
from dlw.db.models.chunk_throughput import ChunkThroughputSample
from dlw.db.models.tenant import Tenant
from dlw.services.capacity_estimator import build_capacity_matrix


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    import dlw.db.models  # noqa: F401
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add(Tenant(id=910, slug="cap", display_name="Cap"))
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
async def _per_test_cleanup(engine):
    yield
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        from sqlalchemy import delete
        await s.execute(delete(ChunkThroughputSample))
        await s.commit()


@pytest.fixture
async def session(engine):
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        yield s


def _seed(executor_id="ex-A", source_id="hf", file_type="safetensors",
          bytes_transferred=1_000_000, duration_ms=1000,
          recorded_at=None, tenant_id=910):
    return ChunkThroughputSample(
        executor_id=executor_id, source_id=source_id, file_type=file_type,
        bytes_transferred=bytes_transferred, duration_ms=duration_ms,
        recorded_at=recorded_at, tenant_id=tenant_id)


async def test_no_samples_returns_empty_matrix(session):
    out = await build_capacity_matrix(session)
    assert out == []


async def test_below_min_samples_threshold_excluded(session):
    """A triple with fewer than min_samples_per_key entries should be
    omitted — one sample is too noisy to bet on."""
    session.add(_seed())  # 1 sample only
    await session.commit()
    out = await build_capacity_matrix(session, min_samples_per_key=3)
    assert out == []


async def test_aggregation_uses_median(session):
    """Median is robust to one outlier — verify a 10x spike gets ignored."""
    session.add_all([
        _seed(bytes_transferred=1_000_000, duration_ms=1000),  # 1 MB/s
        _seed(bytes_transferred=1_000_000, duration_ms=1000),  # 1 MB/s
        _seed(bytes_transferred=1_000_000, duration_ms=1000),  # 1 MB/s
        _seed(bytes_transferred=100_000_000, duration_ms=1000),  # 100 MB/s spike
    ])
    await session.commit()
    out = await build_capacity_matrix(session, min_samples_per_key=3)
    assert len(out) == 1
    assert out[0].bytes_per_sec == 1_000_000  # median, not mean


async def test_old_samples_excluded_by_lookback(session):
    """Samples outside the lookback window must not appear."""
    old = datetime.now(UTC) - timedelta(minutes=60)
    session.add_all([
        _seed(recorded_at=old),
        _seed(recorded_at=old),
        _seed(recorded_at=old),
    ])
    await session.commit()
    out = await build_capacity_matrix(session, lookback_minutes=30)
    assert out == []


async def test_buckets_by_executor_source_file_type(session):
    session.add_all([
        _seed(executor_id="ex-A", source_id="hf", file_type="safetensors"),
        _seed(executor_id="ex-A", source_id="hf", file_type="safetensors"),
        _seed(executor_id="ex-A", source_id="hf", file_type="safetensors"),
        _seed(executor_id="ex-B", source_id="hf", file_type="safetensors"),
        _seed(executor_id="ex-B", source_id="hf", file_type="safetensors"),
        _seed(executor_id="ex-B", source_id="hf", file_type="safetensors"),
        _seed(executor_id="ex-A", source_id="hf", file_type="json"),
        _seed(executor_id="ex-A", source_id="hf", file_type="json"),
        _seed(executor_id="ex-A", source_id="hf", file_type="json"),
    ])
    await session.commit()
    out = await build_capacity_matrix(session, min_samples_per_key=3)
    keys = {(c.executor_id, c.source_id, c.file_type) for c in out}
    assert keys == {
        ("ex-A", "hf", "safetensors"),
        ("ex-B", "hf", "safetensors"),
        ("ex-A", "hf", "json"),
    }
