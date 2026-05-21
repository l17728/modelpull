"""AI Copilot read-only tool registry (UI-SP4a). Tools run in-process in
the caller's tenant scope (invariant 15) — they reuse the same
tenant_filtered queries / services as the REST handlers. Audit (invariant
16) is applied by the chat service, not here."""
from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from dlw.auth.principal import Principal
from dlw.db.models.task import DownloadTask
from dlw.db.tenant_scope import tenant_filtered
from dlw.schemas.task import TaskRead
from dlw.services.quota_read import get_quota_snapshot
from dlw.services.task_detail import events_for_task


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    run: Callable[..., Awaitable[dict]]


async def _list_tasks(session: AsyncSession, principal: Principal, *,
                      status: str | None = None, limit: int = 20) -> dict:
    stmt = tenant_filtered(select(DownloadTask), DownloadTask, principal)
    if status:
        stmt = stmt.where(DownloadTask.status == status)
    stmt = stmt.order_by(DownloadTask.created_at.desc()).limit(
        max(1, min(int(limit), 100)))
    rows = (await session.execute(stmt)).scalars().all()
    return {"items": [TaskRead.model_validate(r).model_dump(mode="json")
                      for r in rows]}


async def _get_task(session: AsyncSession, principal: Principal, *,
                    task_id: str) -> dict:
    tid = uuid.UUID(task_id)
    row = (await session.execute(
        tenant_filtered(select(DownloadTask).where(DownloadTask.id == tid),
                        DownloadTask, principal)
        .options(selectinload(DownloadTask.subtasks)))).scalar_one_or_none()
    if row is None:
        return {"error": "task not found"}
    return TaskRead.model_validate(row).model_dump(mode="json")


async def _get_task_events(session: AsyncSession, principal: Principal, *,
                           task_id: str, limit: int = 20) -> dict:
    tid = uuid.UUID(task_id)
    owned = await session.scalar(
        tenant_filtered(select(DownloadTask.id)
                        .where(DownloadTask.id == tid),
                        DownloadTask, principal))
    if owned is None:
        return {"error": "task not found"}
    items, next_cursor = await events_for_task(
        session, tid, principal.tenant_id,
        max(1, min(int(limit), 50)), None)
    return {"items": [it.model_dump(mode="json") for it in items],
            "next_cursor": next_cursor}


async def _quota_current(session: AsyncSession, principal: Principal) -> dict:
    snap = await get_quota_snapshot(session, principal.tenant_id)
    return snap or {"error": "tenant not found"}


READONLY_TOOLS: dict[str, Tool] = {
    "dlw_list_tasks": Tool(
        "dlw_list_tasks",
        "List the caller's download tasks (optionally filtered by status).",
        {"type": "object", "properties": {
            "status": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100}}},
        _list_tasks),
    "dlw_get_task": Tool(
        "dlw_get_task",
        "Get one download task by id (uuid).",
        {"type": "object", "required": ["task_id"], "properties": {
            "task_id": {"type": "string"}}},
        _get_task),
    "dlw_get_task_events": Tool(
        "dlw_get_task_events",
        "Get recent events for a task.",
        {"type": "object", "required": ["task_id"], "properties": {
            "task_id": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50}}},
        _get_task_events),
    "dlw_quota_current": Tool(
        "dlw_quota_current",
        "Get the caller tenant's current quota usage.",
        {"type": "object", "properties": {}},
        _quota_current),
}
