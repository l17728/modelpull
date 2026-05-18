"""Thin audit-log writer over the existing AuditLog model (Phase 3 SP1).
Phase 4 adds prev_hash/self_hash chaining; SP1 only records rows."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.audit import AuditLog


async def write_audit(
    session: AsyncSession, *, action: str, resource_type: str,
    resource_id: str | None, outcome: str, tenant_id: int | None,
    actor_user_id: int | None, payload: dict[str, Any] | None = None,
) -> None:
    body = json.dumps(
        {"action": action, "resource_id": resource_id, "outcome": outcome,
         "payload": payload}, sort_keys=True, default=str)
    session.add(AuditLog(
        tenant_id=tenant_id, actor_user_id=actor_user_id, action=action,
        resource_type=resource_type, resource_id=resource_id,
        outcome=outcome, payload=payload,
        self_hash=hashlib.sha256(body.encode()).hexdigest()))
