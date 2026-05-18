"""Task request/response DTOs."""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from dlw.schemas.subtask import SubTaskRead


class TaskCreate(BaseModel):
    """POST /api/v1/tasks request body."""
    repo_id: str = Field(min_length=1, max_length=256, examples=["deepseek-ai/DeepSeek-V3"])
    revision: str = Field(min_length=1, max_length=64, examples=["abc123def4567890" * 2 + "abc12345"])
    storage_id: int = Field(gt=0)
    path_template: str = Field(default="{tenant}/{repo_id}/{revision}", max_length=512)
    priority: int = Field(default=1, ge=0, le=10)
    source_strategy: str = Field(default="auto_balance", max_length=32)
    source_blacklist: list[str] = Field(default_factory=list)
    trust_non_hf_sha256: bool = Field(default=False)


class TaskRead(BaseModel):
    """Slim list-item shape — no subtasks (avoids N+1 across many tasks)."""
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    repo_id: str
    revision: str
    status: str
    priority: int
    created_at: datetime
    completed_at: datetime | None
    error_message: str | None


class TaskDetail(TaskRead):
    """GET /api/v1/tasks/{id} response — adds subtasks list."""
    subtasks: list[SubTaskRead] = []


class TaskList(BaseModel):
    """GET /api/v1/tasks response body."""
    items: list[TaskRead]
    total: int
