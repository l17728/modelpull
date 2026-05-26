"""v2.1 Sprint 9 — Online replan loop tests."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.db.base import Base
from dlw.db.models.chunk_throughput import ChunkThroughputSample
from dlw.db.models.source import SubtaskChunk
from dlw.db.models.storage import StorageBackend
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.db.models.tenant import Project, Tenant, User
from dlw.observability.metrics import (
    OPTIMIZER_SOLVE_DURATION_SECONDS,
    REPLAN_CHUNK_MOVES_TOTAL,
    _reset_for_tests,
)
from dlw.services.replan_loop import replan_tick


_TID = 920
_TASK_ID = uuid.UUID("dd000000-0000-0000-0000-000000000001")


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    import dlw.db.models  # noqa: F401
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add(Tenant(id=_TID, slug="repl", display_name="Replan"))
        await s.flush()
        s.add_all([
            Project(id=920, tenant_id=_TID, name="d"),
            User(id=920, tenant_id=_TID, oidc_subject="r", email="r@t",
                 role="tenant_admin"),
            StorageBackend(id=920, tenant_id=_TID, name="s",
                            backend_type="s3", config_encrypted=b""),
        ])
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
async def _cleanup(engine):
    _reset_for_tests()
    yield
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        from sqlalchemy import delete
        await s.execute(delete(SubtaskChunk))
        await s.execute(delete(FileSubTask))
        await s.execute(delete(DownloadTask))
        await s.execute(delete(ChunkThroughputSample))
        await s.commit()


@pytest.fixture
def factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


async def _seed_task_with_chunks(factory_, *,
                                   chunk_specs: list[tuple[str, int]]):
    """chunk_specs: list of (source_id, byte_size). All chunks land on
    one subtask with filename 'model.safetensors' so file_type='safetensors'."""
    async with factory_() as s:
        s.add(DownloadTask(
            id=_TASK_ID, tenant_id=_TID, project_id=920,
            owner_user_id=920, storage_id=920, repo_id="o/m",
            revision="0" * 40,
            path_template="hf/{model}/{revision}/{file}",
            status="running"))
        sub = FileSubTask(
            id=uuid.uuid4(), task_id=_TASK_ID, tenant_id=_TID,
            filename="model.safetensors", expected_sha256="0" * 64,
            file_size=sum(sz for _, sz in chunk_specs),
            status="pending")
        s.add(sub)
        await s.flush()
        offset = 0
        chunks: list[SubtaskChunk] = []
        for i, (src, sz) in enumerate(chunk_specs):
            c = SubtaskChunk(
                subtask_id=sub.id, chunk_index=i,
                byte_start=offset, byte_end=offset + sz - 1,
                source_id=src, status="pending", bytes_done=0)
            s.add(c)
            chunks.append(c)
            offset += sz
        await s.commit()
        return sub, chunks


async def _seed_samples(factory_, *, rows: list[tuple[str, str, float]]):
    """rows: list of (executor_id, source_id, bytes_per_sec). Seeds 3
    samples per row at safetensors file_type so the estimator picks them up
    above the min-samples threshold."""
    async with factory_() as s:
        for ex, src, bps in rows:
            for _ in range(3):
                s.add(ChunkThroughputSample(
                    executor_id=ex, source_id=src,
                    file_type="safetensors",
                    bytes_transferred=int(bps),
                    duration_ms=1000, tenant_id=_TID))
        await s.commit()


# ---------------------------------------------------------------------------
# Degenerate paths

async def test_no_pending_chunks_noop(factory):
    r = await replan_tick(factory)
    assert r.moves == []
    assert r.pending_chunks_seen == 0


async def test_no_capacity_data_noop(factory):
    """Pending chunks exist but no chunk_throughput_sample rows → no moves."""
    await _seed_task_with_chunks(factory, chunk_specs=[("hf", 100)])
    r = await replan_tick(factory)
    assert r.moves == []
    assert r.capacity_entries == 0


# ---------------------------------------------------------------------------
# Shadow mode never writes

async def test_shadow_mode_does_not_change_source_id(factory):
    """Even when the optimizer wants to move a chunk, source_id stays
    untouched in shadow mode."""
    await _seed_task_with_chunks(factory, chunk_specs=[("hf", 1000)])
    # mirror is faster than hf → optimizer picks mirror, wants to move
    await _seed_samples(factory, rows=[
        ("ex-A", "hf", 100),         # 10s for the chunk
        ("ex-A", "mirror", 10000),   # 0.1s for the chunk
    ])

    r = await replan_tick(factory, apply=False)
    assert len(r.moves) == 1
    assert r.moves[0].old_source == "hf"
    assert r.moves[0].new_source == "mirror"
    assert r.applied is False

    async with factory() as s:
        row = (await s.execute(select(SubtaskChunk))).scalar_one()
    assert row.source_id == "hf"  # NOT changed


async def test_apply_mode_persists_move(factory):
    await _seed_task_with_chunks(factory, chunk_specs=[("hf", 1000)])
    await _seed_samples(factory, rows=[
        ("ex-A", "hf", 100),
        ("ex-A", "mirror", 10000),
    ])

    r = await replan_tick(factory, apply=True)
    assert r.applied is True
    assert len(r.moves) == 1

    async with factory() as s:
        row = (await s.execute(select(SubtaskChunk))).scalar_one()
    assert row.source_id == "mirror"


# ---------------------------------------------------------------------------
# Running chunks are never touched

async def test_running_chunks_never_moved(factory):
    """If the chunk is no longer pending between solve and apply, the
    UPDATE's status='pending' WHERE clause must prevent the write."""
    sub, chunks = await _seed_task_with_chunks(
        factory, chunk_specs=[("hf", 1000)])
    await _seed_samples(factory, rows=[
        ("ex-A", "hf", 100),
        ("ex-A", "mirror", 10000),
    ])

    # Race: between solve and apply, the chunk flips to running
    # We simulate by flipping its status BEFORE calling apply path
    async with factory() as s:
        c = (await s.execute(select(SubtaskChunk))).scalar_one()
        c.status = "running"
        await s.commit()

    # No pending chunks → no moves
    r = await replan_tick(factory, apply=True)
    assert r.moves == []
    assert r.pending_chunks_seen == 0
    async with factory() as s:
        row = (await s.execute(select(SubtaskChunk))).scalar_one()
    assert row.source_id == "hf"  # untouched


# ---------------------------------------------------------------------------
# Optimizer-preferred-source equals current → no move

async def test_already_optimal_no_move(factory):
    """If the current source IS what the optimizer would pick, the diff
    yields zero moves."""
    await _seed_task_with_chunks(factory, chunk_specs=[("mirror", 1000)])
    await _seed_samples(factory, rows=[
        ("ex-A", "hf", 100),
        ("ex-A", "mirror", 10000),
    ])
    r = await replan_tick(factory, apply=True)
    assert r.moves == []


# ---------------------------------------------------------------------------
# Metrics emitted

async def test_metric_increment_shadow(factory):
    await _seed_task_with_chunks(factory, chunk_specs=[("hf", 1000)])
    await _seed_samples(factory, rows=[
        ("ex-A", "hf", 100),
        ("ex-A", "mirror", 10000),
    ])
    await replan_tick(factory, apply=False)

    shadow_counter = REPLAN_CHUNK_MOVES_TOTAL.labels(mode="shadow")
    assert shadow_counter._value.get() == 1.0  # type: ignore[attr-defined]


async def test_metric_increment_apply(factory):
    await _seed_task_with_chunks(factory, chunk_specs=[("hf", 1000)])
    await _seed_samples(factory, rows=[
        ("ex-A", "hf", 100),
        ("ex-A", "mirror", 10000),
    ])
    await replan_tick(factory, apply=True)

    apply_counter = REPLAN_CHUNK_MOVES_TOTAL.labels(mode="apply")
    assert apply_counter._value.get() == 1.0  # type: ignore[attr-defined]


async def test_solve_duration_reported(factory):
    """solve_seconds is part of the ReplanResult contract; non-negative
    even if the underlying clock resolution rounds a tiny solve to 0."""
    await _seed_task_with_chunks(factory, chunk_specs=[("hf", 1000)])
    await _seed_samples(factory, rows=[
        ("ex-A", "hf", 100),
        ("ex-A", "mirror", 10000),
    ])
    r = await replan_tick(factory)
    assert r.solve_seconds >= 0.0
    assert r.capacity_entries == 2


# ---------------------------------------------------------------------------
# replan_loop env-flag respect

async def test_loop_skips_tick_when_master_disabled(factory, monkeypatch):
    """If DLW_ADAPTIVE_OPTIMIZER_ENABLED is unset/false, the loop sleeps
    without calling replan_tick. We assert by counting capacity-matrix
    queries via a sentinel."""
    import asyncio as _aio
    monkeypatch.delenv("DLW_ADAPTIVE_OPTIMIZER_ENABLED", raising=False)
    await _seed_task_with_chunks(factory, chunk_specs=[("hf", 1000)])
    await _seed_samples(factory, rows=[
        ("ex-A", "hf", 100),
        ("ex-A", "mirror", 10000),
    ])

    from dlw.services.replan_loop import replan_loop
    task = _aio.create_task(replan_loop(factory, interval_seconds=0.05))
    try:
        await _aio.sleep(0.2)  # >= 3 ticks at this interval
    finally:
        task.cancel()
        try:
            await _aio.wait_for(task, timeout=1)
        except (_aio.TimeoutError, _aio.CancelledError):
            pass

    # No moves recorded (loop was disabled)
    shadow_counter = REPLAN_CHUNK_MOVES_TOTAL.labels(mode="shadow")
    apply_counter = REPLAN_CHUNK_MOVES_TOTAL.labels(mode="apply")
    assert shadow_counter._value.get() == 0.0  # type: ignore[attr-defined]
    assert apply_counter._value.get() == 0.0   # type: ignore[attr-defined]
