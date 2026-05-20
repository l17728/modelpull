"""GET /api/v1/executors/stream — SSE executors-list stream (UI-SP5b).

NOT in src/dlw/api/executors.py because that file is mTLS-only per
tools/lint_invariants.py:check_no_bearer_on_executor_routes. Uses require_perm.
Same hand-rolled text/event-stream idiom as tasks_stream.py.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.auth.principal import Principal
from dlw.authz.deps import require_perm
from dlw.config import get_settings
from dlw.db.session import get_engine
from dlw.schemas.executor_read import ExecutorListResponse, ExecutorRead
from dlw.services.executors_read import list_executors_for_principal

router = APIRouter(prefix="/api/v1/executors", tags=["executors"])

_StatusLit = Literal["joining", "healthy", "degraded", "suspect", "faulty"]

_KEEPALIVE_EVERY_TICKS = 6  # vestigial (cf. SP5); kept for parity


def _clamped_interval() -> float:
    raw = float(getattr(
        get_settings(), "executors_stream_interval_seconds", 5.0))
    return max(0.5, min(60.0, raw))


@router.get("/stream")
async def stream_executors(
    request: Request,
    status: _StatusLit | None = Query(default=None),
    max_ticks: int | None = Query(default=None, ge=1, le=10000),
    principal: Principal = Depends(require_perm("/api/v1/executors*", "GET")),
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
                    rows = await list_executors_for_principal(
                        s, principal, status)
                payload = ExecutorListResponse(
                    items=[ExecutorRead.model_validate(r) for r in rows])
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
