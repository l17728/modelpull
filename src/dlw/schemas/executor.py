"""Executor request/response DTOs."""
from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from dlw.schemas.storage import StorageConfig
from dlw.schemas.subtask import SubTaskRead


class ExecutorRegister(BaseModel):
    host_id: str
    executor_id_proposal: str
    capabilities: dict[str, Any] = {}
    client_csr_pem: str


class RegistrationResponse(BaseModel):
    executor_id: str
    epoch: int
    client_cert_pem: str
    ca_chain: list[str]
    executor_jwt: str
    hmac_seed_hex: str
    cert_renew_in_seconds: int
    jwt_renew_in_seconds: int


class RenewRequest(BaseModel):
    client_csr_pem: str | None = None


class RenewResponse(BaseModel):
    executor_jwt: str
    jwt_renew_in_seconds: int
    client_cert_pem: str | None = None
    cert_renew_in_seconds: int | None = None


class ExecutorHeartbeat(BaseModel):
    """POST /api/v1/executors/{id}/heartbeat — periodic liveness ping."""
    health_score: int = Field(default=100, ge=0, le=100)
    parts_dir_bytes: int = Field(default=0, ge=0)
    # W2b1: optional disk capacity report; None means "no update" (backward-compat).
    disk_free_gb: int | None = Field(default=None, ge=0)


class ExecutorRead(BaseModel):
    """Returned by join/heartbeat to confirm registration."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    health_score: int
    epoch: int        # NEW (P2-W1 fence: clients persist this from /join response)


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
