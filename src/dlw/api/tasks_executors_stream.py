"""GET /api/v1/tasks/{task_id}/participating-executors/stream — SSE live stream (UI-SP5i).

Hand-rolled text/event-stream; reuses SP2's executors_for_task service
(list), wrapped as ParticipatingExecutors. Mirrors tasks_chunks_stream.py.
"""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.auth.principal import Principal
from dlw.authz.deps import require_perm
from dlw.config import get_settings
from dlw.db.models.task import DownloadTask
from dlw.db.session import get_engine
from dlw.db.tenant_scope import tenant_filtered
from dlw.schemas.task_detail import ParticipatingExecutors
from dlw.services import task_detail as _td

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


def _clamped_interval() -> float:
    raw = float(getattr(
        get_settings(), "task_executors_stream_interval_seconds", 2.0))
    return max(0.5, min(60.0, raw))


@router.get("/{task_id}/participating-executors/stream")
async def stream_participating_executors(
    task_id: uuid.UUID,
    request: Request,
    max_ticks: int | None = Query(default=None, ge=1, le=10000),
    principal: Principal = Depends(require_perm("/api/v1/tasks*", "GET")),
) -> StreamingResponse:
    session_maker = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with session_maker() as s:
        owned = await s.scalar(
            tenant_filtered(select(DownloadTask.id)
                            .where(DownloadTask.id == task_id),
                            DownloadTask, principal))
    if owned is None:
        raise HTTPException(status_code=404, detail="task not found")

    interval = _clamped_interval()

    async def _body() -> AsyncIterator[bytes]:
        yield b":open\n\n"
        tick_count = 0
        try:
            while True:
                if await request.is_disconnected():
                    return
                async with session_maker() as s:
                    items = await _td.executors_for_task(
                        s, task_id, principal.tenant_id)
                payload = ParticipatingExecutors(items=items)
                yield (f"data: {payload.model_dump_json()}"
                       "\n\n").encode("utf-8")
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
