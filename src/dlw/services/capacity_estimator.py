"""v2.1 Sprint 8 — Capacity matrix builder.

Reads recent rows from chunk_throughput_sample (Sprint 7) and aggregates
them into (executor, source, file_type) → bytes/sec rows the optimizer
can consume. Uses the median across the lookback window so transient
spikes don't poison the LP."""
from __future__ import annotations

import logging
import statistics
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.chunk_throughput import ChunkThroughputSample
from dlw.services.optimizer import Capacity

logger = logging.getLogger(__name__)

DEFAULT_LOOKBACK_MINUTES = 30
DEFAULT_MIN_SAMPLES_PER_KEY = 3


async def build_capacity_matrix(
    session: AsyncSession, *,
    lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES,
    min_samples_per_key: int = DEFAULT_MIN_SAMPLES_PER_KEY,
) -> list[Capacity]:
    """Returns a list of Capacity rows ready to pass to optimizer.solve().

    Only (executor, source, file_type) triples with at least
    `min_samples_per_key` samples in the window are included — a single
    sample is too noisy to bet a scheduling decision on. Triples with
    fewer samples are simply omitted; the optimizer treats missing
    entries as zero capacity (chunk won't be placed there)."""
    cutoff = datetime.now(UTC) - timedelta(minutes=lookback_minutes)
    stmt = (select(ChunkThroughputSample)
            .where(ChunkThroughputSample.recorded_at >= cutoff))
    rows = (await session.execute(stmt)).scalars().all()

    # Bucket by (executor, source, file_type) → list of bps
    buckets: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for r in rows:
        if r.duration_ms <= 0 or r.bytes_transferred <= 0:
            continue
        bps = r.bytes_transferred / (r.duration_ms / 1000.0)
        buckets[(r.executor_id, r.source_id, r.file_type)].append(bps)

    out: list[Capacity] = []
    for (ex, src, ft), bps_list in buckets.items():
        if len(bps_list) < min_samples_per_key:
            continue
        median_bps = statistics.median(bps_list)
        if median_bps <= 0:
            continue
        out.append(Capacity(
            executor_id=ex, source_id=src, file_type=ft,
            bytes_per_sec=median_bps))

    logger.debug(
        "capacity_estimator: %d sample rows → %d capacity entries (lookback=%dm)",
        len(rows), len(out), lookback_minutes)
    return out
