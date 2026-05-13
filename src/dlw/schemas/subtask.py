"""SubTask request/response DTOs."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SubTaskRead(BaseModel):
    """Returned in assignment payload + GET subtask detail."""
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task_id: uuid.UUID
    filename: str
    file_size: int | None
    expected_sha256: str | None
    status: str
    s3_key: str | None = Field(default=None, max_length=1024)


class SubTaskReport(BaseModel):
    """POST /api/v1/subtasks/{id}/report request body — executor reports outcome."""
    status: Literal["succeeded", "failed", "paused_disk_full"]
    assignment_token: uuid.UUID | None = Field(
        default=None,
        description="Token from /poll's AssignmentResponse — verified against "
                    "stored value to defend against stale/forged reports (W2-F).",
    )
    actual_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    bytes_downloaded: int = Field(default=0, ge=0)
    s3_key: str | None = Field(default=None, max_length=1024)
    error: str | None = Field(default=None, max_length=2048)
    executor_epoch: int | None = Field(
        default=None,
        description="Executor's current epoch (fence). Must match subtask.executor_epoch.",
    )
