"""UI-SP2 Task-Detail read-only DTOs (additive; mirrors api/openapi.yaml)."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class ChunkSeg(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    chunk_index: int
    byte_start: int
    byte_end: int
    source_id: str
    status: str
    bytes_done: int


class SubtaskChunkRow(BaseModel):
    subtask_id: uuid.UUID
    filename: str
    file_size: int | None
    status: str
    bytes_downloaded: int
    is_chunked: bool
    chunks_total: int | None
    chunks_completed: int
    chunks: list[ChunkSeg]


class SubtaskChunkReport(BaseModel):
    items: list[SubtaskChunkRow]


class SourceUsed(BaseModel):
    source_id: str
    bytes_assigned: int
    percent: float
    measured_speed_bps: float


class ChunkRouting(BaseModel):
    filename: str
    chunks: list[ChunkSeg]


class SourceAllocation(BaseModel):
    task_id: uuid.UUID
    sources_used: list[SourceUsed]
    chunk_level_routing: list[ChunkRouting]


class ParticipatingExecutor(BaseModel):
    executor_id: str
    executor_status: str | None
    health_score: int | None
    last_heartbeat_at: datetime | None
    assigned_subtasks: int
    active_subtasks: int
    bytes_downloaded: int


class ParticipatingExecutors(BaseModel):
    items: list[ParticipatingExecutor]


class TaskEvent(BaseModel):
    ts: datetime
    type: str
    message: str
    details: dict[str, Any]


class TaskEventsResponse(BaseModel):
    items: list[TaskEvent]
    next_cursor: str | None = None
