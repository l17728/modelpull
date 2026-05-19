"""UI-SP3 Executor list read-only DTOs (additive)."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ExecutorRead(BaseModel):
    """Browser-facing executor shape (matches openapi.yaml `ExecutorRead`)."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    health_score: int
    epoch: int
    host_id: str | None
    tenant_id: int | None
    last_heartbeat_at: datetime | None
    nic_speed_gbps: int | None
    disk_free_gb: int | None
    disk_total_gb: int | None
    created_at: datetime | None


class ExecutorListResponse(BaseModel):
    items: list[ExecutorRead]
