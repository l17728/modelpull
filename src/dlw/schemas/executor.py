"""Executor request/response DTOs."""
from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from dlw.schemas.storage import StorageConfig
from dlw.schemas.subtask import SubTaskRead


class ExecutorJoin(BaseModel):
    """POST /api/v1/executors/join — first contact from new executor.

    NOTE (W2-H): Invariant 9 in `docs/v2.0/03-distributed-correctness.md`
    requires id format `^[a-z0-9-]+-worker-\\d+$`. Week 2 does NOT enforce this
    at the schema level — tests use shorter ids like 'exec-A' for brevity.
    Phase 2 will add a `@field_validator("id")` enforcing the regex once mTLS
    cert binding is in place (the cert CN should match the executor id).
    """
    id: str = Field(min_length=1, max_length=64, examples=["host-12.local-worker-1"])
    host_id: str = Field(min_length=1, max_length=64)
    cert_fingerprint: str = Field(default="placeholder-week2", max_length=128)
    capabilities: dict[str, Any] = Field(default_factory=dict)


class ExecutorHeartbeat(BaseModel):
    """POST /api/v1/executors/{id}/heartbeat — periodic liveness ping."""
    health_score: int = Field(default=100, ge=0, le=100)
    parts_dir_bytes: int = Field(default=0, ge=0)


class ExecutorRead(BaseModel):
    """Returned by join/heartbeat to confirm registration."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    health_score: int


class AssignmentResponse(BaseModel):
    """POST /api/v1/executors/{id}/poll response — either subtask or empty.

    Phase 1 W4: when assigned, includes repo_id + revision (executor needs to
    construct HF URL) and storage_config (executor needs S3 bucket / endpoint).
    """
    assigned: bool
    subtask: SubTaskRead | None = None
    assignment_token: uuid.UUID | None = None
    # Phase 1 W4 additions (None when assigned=False):
    repo_id: str | None = None
    revision: str | None = None
    storage_config: StorageConfig | None = None
