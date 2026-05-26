"""v2.1 Sprint 7 — Async-batched throughput sampler.

Records one ChunkThroughputSample row per completed chunk. Called from
the chunk-complete path; MUST NOT block: a slow sample write should
never delay the next chunk being picked up.

Design
------
The hot path calls `record_sample(sample)` which appends to an in-memory
deque (no I/O). A background `flush_loop()` running every
`FLUSH_INTERVAL_SECONDS` drains the deque and bulk-inserts.

If the buffer overflows MAX_BUFFER_SIZE between flushes (e.g. controller
DB is briefly unhealthy), oldest samples are dropped and a warning is
logged. That's a deliberate trade — for analytics fidelity we'd queue
forever; for controller stability we cap.

The retention loop (`retention_tick`) deletes rows older than
`retention_days` so the table doesn't grow unbounded."""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.db.models.chunk_throughput import ChunkThroughputSample

logger = logging.getLogger(__name__)

# Tuning constants — also documented in the docstring above.
FLUSH_INTERVAL_SECONDS = 5.0
MAX_BUFFER_SIZE = 10_000  # drop-oldest cap on the in-memory buffer
DEFAULT_RETENTION_DAYS = 7


@dataclass(frozen=True)
class Sample:
    executor_id: str
    source_id: str
    file_type: str
    bytes_transferred: int
    duration_ms: int
    tenant_id: int | None = None


# Module-level singleton buffer. A class-based encapsulation feels cleaner
# but the buffer is genuinely global — multiple call sites in the chunk
# path can hit record_sample() concurrently and we want them feeding the
# same deque without passing a ref everywhere.
_buffer: deque[Sample] = deque(maxlen=MAX_BUFFER_SIZE)
_buffer_lock = asyncio.Lock()


def record_sample(sample: Sample) -> None:
    """Hot-path entry. NEVER blocks on I/O. Deque is bounded so OOM is
    impossible — if writes back up, the OLDEST samples are evicted."""
    if sample.duration_ms <= 0 or sample.bytes_transferred < 0:
        # Caller bug, but better to drop than to commit a check-violating row
        logger.debug("throughput_sampler: dropping malformed sample %r", sample)
        return
    _buffer.append(sample)


async def drain_into(rows_out: list[Sample]) -> int:
    """Empty the buffer into the caller-provided list. Returns count moved.

    Done in one short critical section so the flush task doesn't race with
    high-volume record_sample callers (record_sample itself doesn't take
    the lock — append on a bounded deque is atomic from the GIL's POV).
    """
    async with _buffer_lock:
        n = len(_buffer)
        if n == 0:
            return 0
        rows_out.extend(_buffer)
        _buffer.clear()
        return n


async def flush_once(session_factory: async_sessionmaker) -> int:
    """Single flush pass — drain + bulk insert. Returns rows written."""
    drained: list[Sample] = []
    n = await drain_into(drained)
    if n == 0:
        return 0
    rows = [
        ChunkThroughputSample(
            executor_id=s.executor_id,
            source_id=s.source_id,
            file_type=s.file_type,
            bytes_transferred=s.bytes_transferred,
            duration_ms=s.duration_ms,
            tenant_id=s.tenant_id)
        for s in drained
    ]
    try:
        async with session_factory() as session:
            session.add_all(rows)
            await session.commit()
        logger.debug("throughput_sampler: flushed %d samples", n)
        return n
    except Exception:
        # Insertion failed (DB hiccup, FK gone, schema drift) — don't
        # re-buffer. The samples are advisory; losing a few minutes is fine.
        logger.exception("throughput_sampler: flush failed; %d samples lost", n)
        return 0


async def flush_loop(session_factory: async_sessionmaker, *,
                     interval_seconds: float = FLUSH_INTERVAL_SECONDS) -> None:
    """Background loop. Cancellation-safe."""
    logger.info("throughput_sampler flush_loop started (interval=%.1fs)",
                interval_seconds)
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            await flush_once(session_factory)
        except asyncio.CancelledError:
            # Final-flush on shutdown so we don't drop pending samples
            try:
                await flush_once(session_factory)
            except Exception:
                logger.exception("throughput_sampler: final flush failed")
            raise
        except Exception:
            logger.exception("throughput_sampler tick failed; retrying")


# ---------------------------------------------------------------------------
# Retention

async def retention_tick(session_factory: async_sessionmaker, *,
                          retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
    """Delete rows older than retention_days. Returns rows deleted.

    Cheap because of ix_cts_recorded_at — a range delete with a leading
    index scan. Safe to call frequently; we run it daily."""
    if retention_days <= 0:
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    async with session_factory() as session:
        result = await session.execute(
            delete(ChunkThroughputSample).where(
                ChunkThroughputSample.recorded_at < cutoff))
        deleted = result.rowcount or 0
        await session.commit()
    if deleted:
        logger.info(
            "throughput_sampler: retention deleted %d rows older than %s",
            deleted, cutoff.isoformat())
    return deleted


async def retention_loop(session_factory: async_sessionmaker, *,
                          interval_seconds: float = 86400.0,
                          retention_days: int = DEFAULT_RETENTION_DAYS) -> None:
    """Background loop (daily by default)."""
    logger.info(
        "throughput_sampler retention_loop started "
        "(interval=%.1fs, retention=%d days)",
        interval_seconds, retention_days)
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            await retention_tick(session_factory,
                                  retention_days=retention_days)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("throughput_sampler retention tick failed")


# ---------------------------------------------------------------------------
# Test helpers

def _buffer_for_tests() -> deque[Sample]:
    """Expose buffer so tests can assert state without poking the module."""
    return _buffer


def _reset_for_tests() -> None:
    """Empty the buffer between tests."""
    _buffer.clear()
