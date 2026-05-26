"""v2.1 Sprint 9 — Online replan loop.

Periodically re-solves the chunk-to-(executor, source) assignment using
the latest capacity samples and applies the new plan to pending chunks.
Running chunks are NEVER touched — switch_cost is too high mid-transfer.

Two layers of safety
--------------------
DLW_ADAPTIVE_OPTIMIZER_ENABLED  (default false)
    Master switch. If false, the loop doesn't even run.
DLW_ADAPTIVE_OPTIMIZER_APPLY    (default false)
    Even with the loop enabled, default is SHADOW MODE: compute the
    plan, log + emit metrics, but DO NOT write source_id changes. This
    lets operators see what the optimizer would do for days/weeks
    before flipping APPLY on in production.

The shadow vs apply split mirrors how feature flags should ladder for
risky scheduling changes: observe → A/B → cutover.

Design
------
1. SELECT pending SubtaskChunks (status='pending'). Skip running.
2. Pull a fresh capacity matrix from Sprint 7's samples.
3. Build the Chunk list with file_type from the parent FileSubTask's
   filename (best-effort extension match).
4. solve() returns the assignment plan.
5. Diff: for each pending chunk, if optimizer picks a DIFFERENT
   source than current, that's a "move".
6. If APPLY mode, UPDATE subtask_chunks SET source_id = new_source
   WHERE id IN (moved_ids).
7. Both modes bump the replan_chunk_moves_total counter (mode label)
   and the optimizer_solve_duration_seconds histogram."""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dlw.db.models.source import SubtaskChunk
from dlw.db.models.task import FileSubTask
from dlw.observability.metrics import (
    OPTIMIZER_SOLVE_DURATION_SECONDS,
    REPLAN_CHUNK_MOVES_TOTAL,
)
from dlw.services.capacity_estimator import build_capacity_matrix
from dlw.services.optimizer import Chunk, solve

logger = logging.getLogger(__name__)

DEFAULT_REPLAN_INTERVAL_SECONDS = 30.0


# File extension → file_type bucket. Same map as api/subtasks.py but
# inlined to avoid a cross-layer import.
_FILE_TYPE_MAP = {
    ".safetensors": "safetensors", ".bin": "bin", ".pt": "bin",
    ".gguf": "gguf", ".json": "json", ".txt": "text", ".md": "text",
    ".yaml": "text", ".yml": "text",
}


def _file_type_from_name(name: str | None) -> str:
    if not name:
        return "other"
    lower = name.lower()
    for ext, kind in _FILE_TYPE_MAP.items():
        if lower.endswith(ext):
            return kind
    return "other"


@dataclass(frozen=True)
class ReplanMove:
    """One chunk the optimizer wants to relocate to a different source."""
    chunk_id: int
    subtask_id: Any
    old_source: str
    new_source: str
    new_executor: str  # advisory; subtask-level executor is set elsewhere


@dataclass
class ReplanResult:
    moves: list[ReplanMove]
    solve_seconds: float
    pending_chunks_seen: int
    capacity_entries: int
    applied: bool = False


async def _collect_pending(
    session: AsyncSession,
) -> tuple[list[Chunk], dict[int, tuple[SubtaskChunk, str]]]:
    """Read pending chunks + their parent subtask's filename for file_type
    inference. Returns (chunks_for_optimizer, {chunk.id: (chunk, filename)})."""
    stmt = (
        select(SubtaskChunk, FileSubTask.filename)
        .join(FileSubTask, FileSubTask.id == SubtaskChunk.subtask_id)
        .where(SubtaskChunk.status == "pending"))
    rows = (await session.execute(stmt)).all()

    chunks: list[Chunk] = []
    raw_map: dict[int, tuple[SubtaskChunk, str]] = {}
    for sc, filename in rows:
        # Chunk id must be a string for the optimizer's dict key — convert
        # back at apply time
        size = max(0, sc.byte_end - sc.byte_start + 1)
        chunks.append(Chunk(
            id=str(sc.id), size_bytes=size,
            file_type=_file_type_from_name(filename)))
        raw_map[sc.id] = (sc, filename or "")
    return chunks, raw_map


async def _executor_ids_with_capacity(
    capacities: list[Any],
) -> list[str]:
    """Distinct executor ids appearing in the capacity matrix — these are
    the only ones the optimizer is allowed to assign to."""
    return sorted({c.executor_id for c in capacities})


async def _source_ids_with_capacity(
    capacities: list[Any],
) -> list[str]:
    return sorted({c.source_id for c in capacities})


async def replan_tick(
    session_factory: async_sessionmaker, *,
    apply: bool = False,
    capacity_lookback_minutes: int = 30,
) -> ReplanResult:
    """One replan pass. Always safe to call (returns empty plan if no
    pending chunks or no capacity data)."""
    import time as _t
    async with session_factory() as session:
        chunks, raw_map = await _collect_pending(session)
        if not chunks:
            return ReplanResult(moves=[], solve_seconds=0.0,
                                 pending_chunks_seen=0,
                                 capacity_entries=0, applied=False)

        capacities = await build_capacity_matrix(
            session, lookback_minutes=capacity_lookback_minutes)
        if not capacities:
            return ReplanResult(moves=[], solve_seconds=0.0,
                                 pending_chunks_seen=len(chunks),
                                 capacity_entries=0, applied=False)

        executors = await _executor_ids_with_capacity(capacities)
        sources = await _source_ids_with_capacity(capacities)

    # Solve OUTSIDE the session — the optimizer is CPU-bound and we don't
    # want the read transaction held open during it.
    t0 = _t.monotonic()
    result = solve(chunks, executors, sources, capacities)
    solve_seconds = _t.monotonic() - t0
    try:
        OPTIMIZER_SOLVE_DURATION_SECONDS.observe(solve_seconds)
    except Exception:  # noqa: BLE001
        logger.exception("optimizer solve metric failed; ignoring")

    moves: list[ReplanMove] = []
    for chunk_id_str, (new_ex, new_src) in result.assignments.items():
        try:
            chunk_id = int(chunk_id_str)
        except ValueError:
            continue
        if chunk_id not in raw_map:
            continue
        sc, _filename = raw_map[chunk_id]
        if sc.source_id != new_src:
            moves.append(ReplanMove(
                chunk_id=chunk_id,
                subtask_id=sc.subtask_id,
                old_source=sc.source_id,
                new_source=new_src,
                new_executor=new_ex))

    mode = "apply" if apply else "shadow"
    if moves:
        try:
            REPLAN_CHUNK_MOVES_TOTAL.labels(mode=mode).inc(len(moves))
        except Exception:  # noqa: BLE001
            logger.exception("replan move metric failed; ignoring")

    if apply and moves:
        # One UPDATE per move so different chunks can land on different
        # sources — a CASE expr could batch this but the number of pending
        # chunks at any moment is bounded by the scheduler's lookahead.
        async with session_factory() as session:
            for m in moves:
                # Re-check status='pending' inside the apply tx so we don't
                # clobber a chunk that started running between solve and apply.
                await session.execute(
                    update(SubtaskChunk)
                    .where(SubtaskChunk.id == m.chunk_id,
                            SubtaskChunk.status == "pending")
                    .values(source_id=m.new_source))
            await session.commit()
        logger.info("replan applied %d moves at %s",
                    len(moves), datetime.now(UTC).isoformat())
    else:
        logger.info(
            "replan shadow: would move %d/%d pending chunks "
            "(solve=%.3fs, %d capacity rows)",
            len(moves), len(chunks), solve_seconds, len(capacities))

    return ReplanResult(
        moves=moves, solve_seconds=solve_seconds,
        pending_chunks_seen=len(chunks),
        capacity_entries=len(capacities), applied=apply and bool(moves))


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


async def replan_loop(
    session_factory: async_sessionmaker, *,
    interval_seconds: float = DEFAULT_REPLAN_INTERVAL_SECONDS,
) -> None:
    """Long-running background task. Reads feature flags at every tick so
    operators can flip ENABLED off without restarting the controller."""
    logger.info(
        "replan_loop started (interval=%.1fs); flags read at each tick",
        interval_seconds)
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            if not _env_flag("DLW_ADAPTIVE_OPTIMIZER_ENABLED"):
                continue
            apply = _env_flag("DLW_ADAPTIVE_OPTIMIZER_APPLY")
            await replan_tick(session_factory, apply=apply)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("replan_loop tick failed; retrying")
