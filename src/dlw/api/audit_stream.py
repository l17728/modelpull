"""GET /api/v1/audit/log/stream — SSE audit-log live page-1 stream (UI-SP5d).

Hand-rolled text/event-stream; reuses SP3's search_audit_log service.
Stream always passes cursor=None (live = page 1); the client's "Load older"
button keeps using the one-shot /audit/log?cursor=… endpoint.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.auth.principal import Principal
from dlw.authz.deps import require_perm
from dlw.config import get_settings
from dlw.db.session import get_engine
from dlw.schemas.audit import AuditSearchResponse
from dlw.services.audit_query import search_audit_log

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])

_KEEPALIVE_EVERY_TICKS = 3  # vestigial (cf. SP5+); kept for parity


def _clamped_interval() -> float:
    raw = float(getattr(
        get_settings(), "audit_stream_interval_seconds", 10.0))
    return max(0.5, min(60.0, raw))


@router.get("/log/stream")
async def stream_audit_log(
    request: Request,
    actor_user_id: int | None = Query(default=None),
    action: str | None = Query(default=None, description="prefix match"),
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    max_ticks: int | None = Query(default=None, ge=1, le=10000),
    principal: Principal = Depends(require_perm("/api/v1/audit*", "GET")),
) -> StreamingResponse:
    session_maker = async_sessionmaker(get_engine(), expire_on_commit=False)
    interval = _clamped_interval()

    async def _body() -> AsyncIterator[bytes]:
        yield b":open\n\n"
        ticks_since_data = 0
        tick_count = 0
        try:
            while True:
                if await request.is_disconnected():
                    return
                async with session_maker() as s:
                    items, next_cursor = await search_audit_log(
                        s, principal.tenant_id,
                        actor_user_id=actor_user_id,
                        action_prefix=action,
                        from_=from_, to=to, cursor=None, limit=50)
                payload = AuditSearchResponse(
                    items=items, next_cursor=next_cursor)
                yield (f"data: {payload.model_dump_json()}"
                       "\n\n").encode("utf-8")
                ticks_since_data = 0
                tick_count += 1
                if max_ticks is not None and tick_count >= max_ticks:
                    return
                slept = 0.0
                while slept < interval:
                    if await request.is_disconnected():
                        return
                    chunk = min(0.05, interval - slept)
                    await asyncio.sleep(chunk)
                    slept += chunk
                ticks_since_data += 1
                if ticks_since_data >= _KEEPALIVE_EVERY_TICKS:
                    yield b":keepalive\n\n"
                    ticks_since_data = 0
        except asyncio.CancelledError:
            return

    return StreamingResponse(
        _body(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
