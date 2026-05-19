"""UI-SP2 read-only aggregation helpers (additive; no writes, no state)."""
from __future__ import annotations

import base64
import uuid
from datetime import datetime

from sqlalchemy import and_, false, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.audit import AuditLog
from dlw.db.models.executor import Executor
from dlw.db.models.source import SubtaskChunk
from dlw.db.models.task import FileSubTask
from dlw.schemas.task_detail import (
    ChunkRouting,
    ChunkSeg,
    ParticipatingExecutor,
    SourceAllocation,
    SourceUsed,
    SubtaskChunkRow,
    TaskEvent,
)

_TERMINAL_SUBTASK = {"succeeded", "failed", "cancelled"}


async def chunks_for_task(
    session: AsyncSession, task_id: uuid.UUID, tenant_id: int,
) -> list[SubtaskChunkRow]:
    subs = (await session.execute(
        select(FileSubTask)
        .where(FileSubTask.task_id == task_id,
               FileSubTask.tenant_id == tenant_id)
        .order_by(FileSubTask.filename))).scalars().all()
    if not subs:
        return []
    sub_ids = [s.id for s in subs]
    chunk_rows = (await session.execute(
        select(SubtaskChunk)
        .where(SubtaskChunk.subtask_id.in_(sub_ids))
        .order_by(SubtaskChunk.subtask_id, SubtaskChunk.chunk_index)
    )).scalars().all()
    by_sub: dict[uuid.UUID, list[ChunkSeg]] = {}
    for c in chunk_rows:
        by_sub.setdefault(c.subtask_id, []).append(ChunkSeg.model_validate(c))
    return [
        SubtaskChunkRow(
            subtask_id=s.id, filename=s.filename, file_size=s.file_size,
            status=s.status, bytes_downloaded=s.bytes_downloaded,
            is_chunked=s.is_chunked, chunks_total=s.chunks_total,
            chunks_completed=s.chunks_completed,
            chunks=by_sub.get(s.id, []),
        )
        for s in subs
    ]


async def source_allocation_for_task(
    session: AsyncSession, task_id: uuid.UUID, tenant_id: int,
) -> SourceAllocation:
    subs = (await session.execute(
        select(FileSubTask)
        .where(FileSubTask.task_id == task_id,
               FileSubTask.tenant_id == tenant_id)
        .order_by(FileSubTask.filename))).scalars().all()
    sub_ids = [s.id for s in subs]
    chunk_rows = (await session.execute(
        select(SubtaskChunk).where(SubtaskChunk.subtask_id.in_(sub_ids))
        .order_by(SubtaskChunk.subtask_id, SubtaskChunk.chunk_index)
    )).scalars().all() if sub_ids else []

    chunked_sub_ids = {c.subtask_id for c in chunk_rows}
    by_source: dict[str, int] = {}
    for s in subs:
        if s.id in chunked_sub_ids:
            continue  # chunked files counted at chunk granularity below
        if s.source_id:
            by_source[s.source_id] = (
                by_source.get(s.source_id, 0) + int(s.file_size or 0))
    for c in chunk_rows:
        by_source[c.source_id] = (
            by_source.get(c.source_id, 0)
            + int(c.byte_end - c.byte_start + 1))

    total = sum(by_source.values())
    sources_used = [
        SourceUsed(
            source_id=sid, bytes_assigned=b,
            percent=round(b / total * 100.0, 2) if total else 0.0,
            measured_speed_bps=0.0,  # no live speed source; client-derived
        )
        for sid, b in sorted(by_source.items())
    ]

    routing_by_sub: dict[uuid.UUID, list[ChunkSeg]] = {}
    for c in chunk_rows:
        routing_by_sub.setdefault(c.subtask_id, []).append(
            ChunkSeg.model_validate(c))
    name_by_id = {s.id: s.filename for s in subs}
    chunk_level_routing = [
        ChunkRouting(filename=name_by_id[sid], chunks=segs)
        for sid, segs in sorted(
            routing_by_sub.items(), key=lambda kv: name_by_id[kv[0]])
    ]
    return SourceAllocation(
        task_id=task_id, sources_used=sources_used,
        chunk_level_routing=chunk_level_routing)


async def executors_for_task(
    session: AsyncSession, task_id: uuid.UUID, tenant_id: int,
) -> list[ParticipatingExecutor]:
    subs = (await session.execute(
        select(FileSubTask)
        .where(FileSubTask.task_id == task_id,
               FileSubTask.tenant_id == tenant_id,
               FileSubTask.executor_id.isnot(None)))).scalars().all()
    if not subs:
        return []
    agg: dict[str, dict[str, int]] = {}
    for s in subs:
        eid = s.executor_id
        if eid is None:
            continue
        a = agg.setdefault(
            eid, {"assigned": 0, "active": 0, "bytes": 0})
        a["assigned"] += 1
        if s.status not in _TERMINAL_SUBTASK:
            a["active"] += 1
        a["bytes"] += int(s.bytes_downloaded or 0)
    ex_rows = (await session.execute(
        select(Executor).where(Executor.id.in_(list(agg.keys()))))
    ).scalars().all()
    ex_by_id = {e.id: e for e in ex_rows}
    out: list[ParticipatingExecutor] = []
    for eid, a in sorted(agg.items()):
        e = ex_by_id.get(eid)
        out.append(ParticipatingExecutor(
            executor_id=eid,
            executor_status=e.status if e else None,
            health_score=e.health_score if e else None,
            last_heartbeat_at=e.last_heartbeat_at if e else None,
            assigned_subtasks=a["assigned"],
            active_subtasks=a["active"],
            bytes_downloaded=a["bytes"],
        ))
    return out
