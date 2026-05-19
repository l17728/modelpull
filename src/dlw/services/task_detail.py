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
