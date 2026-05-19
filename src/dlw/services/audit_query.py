"""UI-SP3 audit-log search (read-only; tenant-scoped; cursor-paginated)."""
from __future__ import annotations

import base64
from datetime import datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.audit import AuditLog
from dlw.schemas.audit import AuditEntryRead


def _encode_cursor(occurred_at: datetime, row_id: int) -> str:
    raw = f"{occurred_at.isoformat()}|{row_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, int]:
    raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    ts_str, id_str = raw.rsplit("|", 1)
    return datetime.fromisoformat(ts_str), int(id_str)


async def search_audit_log(
    session: AsyncSession, tenant_id: int, *,
    actor_user_id: int | None,
    action_prefix: str | None,
    from_: datetime | None,
    to: datetime | None,
    cursor: str | None,
    limit: int,
) -> tuple[list[AuditEntryRead], str | None]:
    stmt = select(AuditLog).where(AuditLog.tenant_id == tenant_id)
    if actor_user_id is not None:
        stmt = stmt.where(AuditLog.actor_user_id == actor_user_id)
    if action_prefix:
        stmt = stmt.where(AuditLog.action.like(f"{action_prefix}%"))
    if from_ is not None:
        stmt = stmt.where(AuditLog.occurred_at >= from_)
    if to is not None:
        stmt = stmt.where(AuditLog.occurred_at <= to)
    stmt = stmt.order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc())
    if cursor:
        c_ts, c_id = _decode_cursor(cursor)
        stmt = stmt.where(or_(
            AuditLog.occurred_at < c_ts,
            and_(AuditLog.occurred_at == c_ts, AuditLog.id < c_id)))
    rows = (await session.execute(stmt.limit(limit + 1))).scalars().all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        AuditEntryRead(
            id=r.id, occurred_at=r.occurred_at, tenant_id=r.tenant_id,
            actor_user_id=r.actor_user_id,
            actor_ip=str(r.actor_ip) if r.actor_ip is not None else "",
            action=r.action, resource_type=r.resource_type,
            resource_id=r.resource_id, outcome=r.outcome,
            payload=r.payload or {},
            trace_id=r.trace_id or "",
            prev_hash=r.prev_hash, self_hash=r.self_hash,
        )
        for r in rows
    ]
    next_cursor = (
        _encode_cursor(rows[-1].occurred_at, rows[-1].id)
        if has_more and rows else None)
    return items, next_cursor
