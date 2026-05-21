"""AI Copilot WRITE tool registry (UI-SP4b). Each tool requires
confirmation; execute() reuses the same service-layer path as the REST
handler so all validation (incl. invariant 40 on modified input) runs
naturally. Tenant-scoped via `principal` (inv 15); audited by the chat
service in phase 2 (inv 16 + ai_proposed/user_final for inv 40).

execute() FLUSHES (not commits) — run_confirmation owns the single atomic
commit covering the business write + the ai_tool_calls row update."""
from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.auth.principal import Principal
from dlw.config import get_settings
from dlw.db.models.task import DownloadTask
from dlw.db.models.tenant import Project
from dlw.db.tenant_scope import tenant_filtered
from dlw.schemas.task import TaskCreate
from dlw.services.quota import QuotaExceeded, check_quota_for_new_task
from dlw.services.task_service import cancel_task, create_task


@dataclass
class WriteTool:
    name: str
    description: str
    input_schema: dict
    run: Callable[..., Awaitable[dict]]


async def _cancel(session: AsyncSession, principal: Principal, *,
                  task_id: str) -> dict:
    try:
        tid = uuid.UUID(str(task_id))
    except (ValueError, TypeError):
        return {"error": "invalid task_id"}
    # Tenant gate (the only cross-tenant guard — cancel_task is not
    # independently tenant-scoped; it locks by PK).
    owned = await session.scalar(
        tenant_filtered(select(DownloadTask.id).where(DownloadTask.id == tid),
                        DownloadTask, principal))
    if owned is None:
        return {"error": "task not found"}
    try:
        task = await cancel_task(session, tid)
    except LookupError:
        return {"error": "task not found"}
    except ValueError:
        return {"error": "task not cancellable (terminal)"}
    await session.flush()
    return {"task_id": str(task.id), "status": task.status}


async def _create(session: AsyncSession, principal: Principal, *,
                  repo_id: str, revision: str, storage_id: int,
                  priority: int = 1,
                  source_strategy: str = "auto_balance") -> dict:
    from dlw.services.hf_metadata import (EmptyRepo, HfNetworkError,
                                          HfPrivateOrAuthRequired, RepoNotFound)
    try:
        await check_quota_for_new_task(session, principal.tenant_id)
    except QuotaExceeded as e:
        return {"error": "quota_exceeded", "metric": e.metric}
    project_id = await session.scalar(
        select(func.min(Project.id)).where(
            Project.tenant_id == principal.tenant_id))
    if project_id is None:
        return {"error": "tenant has no project"}
    body = TaskCreate(repo_id=repo_id, revision=revision,
                      storage_id=int(storage_id), priority=int(priority),
                      source_strategy=source_strategy)
    settings = get_settings()
    try:
        task = await create_task(
            session, body, owner_user_id=principal.user_id,
            tenant_id=principal.tenant_id, project_id=int(project_id),
            hf_endpoint=settings.hf_endpoint, hf_token=settings.hf_token)
    except RepoNotFound:
        return {"error": "repo or revision not found"}
    except HfPrivateOrAuthRequired:
        return {"error": "repo is private / requires auth"}
    except HfNetworkError:
        return {"error": "huggingface unreachable"}
    except EmptyRepo:
        return {"error": "repo has no files at this revision"}
    await session.flush()
    return {"task_id": str(task.id), "status": task.status,
            "repo_id": task.repo_id, "revision": task.revision}


WRITE_TOOLS: dict[str, WriteTool] = {
    "dlw_cancel_task": WriteTool(
        "dlw_cancel_task", "Cancel a running download task.",
        {"type": "object", "required": ["task_id"],
         "properties": {"task_id": {"type": "string"}}}, _cancel),
    "dlw_create_task": WriteTool(
        "dlw_create_task", "Create a new download task (consumes quota).",
        {"type": "object", "required": ["repo_id", "revision", "storage_id"],
         "properties": {"repo_id": {"type": "string"},
                        "revision": {"type": "string"},
                        "storage_id": {"type": "integer"},
                        "priority": {"type": "integer"},
                        "source_strategy": {"type": "string"}}}, _create),
}
