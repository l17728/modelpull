"""GET /api/v1/tasks/{id}/stream — SSE TaskDetail stream (UI-SP5).

Hand-rolled text/event-stream via StreamingResponse (same idiom as hf_proxy.py).
1 Hz default tick rate; configurable via DLW_TASK_STREAM_INTERVAL_SECONDS
(clamped [0.1, 10.0] in code). Stream terminates on terminal task status,
client disconnect, or controller shutdown.
"""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import selectinload

from dlw.auth.principal import Principal
from dlw.authz.deps import require_perm
from dlw.config import get_settings
from dlw.db.models.task import DownloadTask
from dlw.db.session import get_engine
from dlw.db.tenant_scope import tenant_filtered
from dlw.schemas.task import TaskDetail

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])

_TERMINAL = {"succeeded", "failed", "cancelled"}
_KEEPALIVE_EVERY_TICKS = 15


def _clamped_interval() -> float:
    raw = float(getattr(
        get_settings(), "task_stream_interval_seconds", 1.0))
    return max(0.1, min(10.0, raw))


async def _load_detail(
    session_maker, task_id: uuid.UUID, tenant_id: int,
) -> TaskDetail | None:
    async with session_maker() as s:
        row = (await s.execute(
            select(DownloadTask).where(
                DownloadTask.id == task_id,
                DownloadTask.tenant_id == tenant_id)
            .options(selectinload(DownloadTask.subtasks))
        )).scalar_one_or_none()
        return TaskDetail.model_validate(row) if row is not None else None


@router.get("/{task_id}/stream")
async def stream_task_detail(
    task_id: uuid.UUID,
    request: Request,
    max_ticks: int | None = Query(default=None, ge=1, le=10000),
    principal: Principal = Depends(require_perm("/api/v1/tasks*", "GET")),
) -> StreamingResponse:
    # Tenant gate — proven cancel-pattern. 404 cross-tenant; never leak.
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
        # Pre-review IMPORTANT fix: flush an immediate comment line so the
        # response headers + first byte ship together. Defeats httpx 0.27.x
        # ASGITransport buffering (which would otherwise hold the response
        # until generator close) AND any production reverse-proxy buffering.
        # SSE parser ignores comment lines; tests' "data:" filter also skips.
        yield b":open\n\n"
        ticks_since_data = 0
        tick_count = 0
        try:
            while True:
                if await request.is_disconnected():
                    return
                detail = await _load_detail(
                    session_maker, task_id, principal.tenant_id)
                if detail is None:
                    return
                yield (f"data: {detail.model_dump_json()}"
                       "\n\n").encode("utf-8")
                ticks_since_data = 0
                tick_count += 1
                if detail.status in _TERMINAL:
                    return
                # Testability hatch — `?max_ticks=N` lets tests verify N
                # snapshots arrive and the body terminates (httpx
                # ASGITransport buffers until generator close). Ignored in
                # production calls (no public client sets it).
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
