"""GET /api/v1/tasks/stream — SSE tenant-scoped tasks-list stream (UI-SP5c).

Hand-rolled text/event-stream via StreamingResponse; reuses the existing
list_tasks aggregation logic (tenant_filtered select + total). Same idiom
as tasks_stream.py / executors_stream.py.

NOTE: this router MUST be registered BEFORE tasks_router in main.py so the
static `/stream` path wins over the parameterized `/{task_id}` route (FastAPI
iterates registered routers in include order).
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.auth.principal import Principal
from dlw.authz.deps import require_perm
from dlw.config import get_settings
from dlw.db.models.task import DownloadTask
from dlw.db.session import get_engine
from dlw.db.tenant_scope import tenant_filtered
from dlw.schemas.task import TaskList, TaskRead

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])

_KEEPALIVE_EVERY_TICKS = 6  # vestigial (cf. SP5/SP5b); kept for parity


def _clamped_interval() -> float:
    raw = float(getattr(
        get_settings(), "tasks_list_stream_interval_seconds", 5.0))
    return max(0.5, min(60.0, raw))


@router.get("/stream")
async def stream_tasks_list(
    request: Request,
    max_ticks: int | None = Query(default=None, ge=1, le=10000),
    principal: Principal = Depends(require_perm("/api/v1/tasks*", "GET")),
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
                    rows = (await s.execute(
                        tenant_filtered(
                            select(DownloadTask), DownloadTask, principal)
                        .order_by(DownloadTask.created_at.desc())
                    )).scalars().all()
                    total = await s.scalar(
                        tenant_filtered(
                            select(func.count()).select_from(DownloadTask),
                            DownloadTask, principal))
                payload = TaskList(
                    items=[TaskRead.model_validate(r) for r in rows],
                    total=int(total or 0))
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
