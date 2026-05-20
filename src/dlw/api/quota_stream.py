"""GET /api/v1/quota/current/stream — SSE quota-snapshot live stream (UI-SP5e).

Hand-rolled text/event-stream; reuses the get_quota_snapshot service
shared with the one-shot /api/v1/quota/current endpoint.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.auth.principal import Principal
from dlw.authz.deps import require_perm
from dlw.config import get_settings
from dlw.db.session import get_engine
from dlw.services.quota_read import get_quota_snapshot

router = APIRouter(prefix="/api/v1/quota", tags=["quota"])


def _clamped_interval() -> float:
    raw = float(getattr(
        get_settings(), "quota_stream_interval_seconds", 15.0))
    return max(0.5, min(60.0, raw))


@router.get("/current/stream")
async def stream_quota_current(
    request: Request,
    max_ticks: int | None = Query(default=None, ge=1, le=10000),
    principal: Principal = Depends(require_perm("/api/v1/quota*", "GET")),
) -> StreamingResponse:
    session_maker = async_sessionmaker(get_engine(), expire_on_commit=False)
    interval = _clamped_interval()

    async with session_maker() as s:
        initial = await get_quota_snapshot(s, principal.tenant_id)
    if initial is None:
        raise HTTPException(404, detail="tenant not found")

    async def _body() -> AsyncIterator[bytes]:
        yield b":open\n\n"
        yield (f"data: {json.dumps(initial)}\n\n").encode("utf-8")
        tick_count = 1
        if max_ticks is not None and tick_count >= max_ticks:
            return
        try:
            while True:
                slept = 0.0
                while slept < interval:
                    if await request.is_disconnected():
                        return
                    chunk = min(0.05, interval - slept)
                    await asyncio.sleep(chunk)
                    slept += chunk
                if await request.is_disconnected():
                    return
                async with session_maker() as s:
                    snap = await get_quota_snapshot(
                        s, principal.tenant_id)
                if snap is None:
                    return
                yield (f"data: {json.dumps(snap)}\n\n").encode("utf-8")
                tick_count += 1
                if max_ticks is not None and tick_count >= max_ticks:
                    return
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
