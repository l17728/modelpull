"""UI-SP3 Audit-search read-only DTOs (additive)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class AuditEntryRead(BaseModel):
    """Mirrors api/openapi.yaml AuditEntry (`searchAuditLog` response item).

    Contract declares several fields as non-nullable that the ORM column
    permits NULL; we coerce None -> "" (or {}) to stay contract-faithful.
    """
    model_config = ConfigDict(from_attributes=True)

    id: int
    occurred_at: datetime
    tenant_id: int | None
    actor_user_id: int | None
    actor_ip: str  # contract non-nullable; coerced from None in router
    action: str
    resource_type: str
    resource_id: str | None
    outcome: str
    payload: dict[str, Any]  # contract non-nullable; coerced {} in router
    trace_id: str  # contract non-nullable; coerced "" in router
    prev_hash: str | None
    self_hash: str


class AuditSearchResponse(BaseModel):
    """Response for GET /api/v1/audit/log.

    Matches the on-disk contract (`searchAuditLog`) which UI-SP3 extends in
    Task 1 Step 3 to include `next_cursor` (nullable) for client pagination.
    """
    items: list[AuditEntryRead]
    next_cursor: str | None = None
