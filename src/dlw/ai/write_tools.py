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
                  repo_id: str, revision: str = "main",
                  storage_id: int | None = None,
                  priority: int = 1,
                  source_strategy: str = "auto_balance") -> dict:
    from dlw.services.hf_metadata import (HfNetworkError,
                                          HfPrivateOrAuthRequired, RepoNotFound)
    from dlw.services.task_service import EmptyRepo
    try:
        await check_quota_for_new_task(session, principal.tenant_id)
    except QuotaExceeded as e:
        return {"error": "quota_exceeded", "metric": e.metric}
    project_id = await session.scalar(
        select(func.min(Project.id)).where(
            Project.tenant_id == principal.tenant_id))
    if project_id is None:
        return {"error": "tenant has no project"}
    # Default storage_id resolution: prefer is_default=true entry visible to
    # this tenant (own or global). Lets AI omit storage_id when there's an
    # obvious choice; tools.py:dlw_list_storages exposes the same view so the
    # AI can disambiguate when there's more than one default.
    if storage_id is None:
        from sqlalchemy import or_
        from dlw.db.models.storage import StorageBackend
        sid = await session.scalar(
            select(StorageBackend.id).where(
                or_(StorageBackend.tenant_id == principal.tenant_id,
                    StorageBackend.tenant_id.is_(None)),
                StorageBackend.is_default.is_(True),
            ).order_by(StorageBackend.id).limit(1))
        if sid is None:
            return {"error": "no default storage — call dlw_list_storages "
                             "and pass storage_id explicitly"}
        storage_id = int(sid)
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


async def _delete(session: AsyncSession, principal: Principal, *,
                  task_id: str) -> dict:
    """Delete a terminal task (succeeded/failed/cancelled). Mirrors the
    DELETE /api/v1/tasks/{id} endpoint: tenant-scoped + 409 if not terminal."""
    from sqlalchemy.orm import selectinload
    from dlw.services.storage_objects import deref_subtask
    try:
        tid = uuid.UUID(str(task_id))
    except (ValueError, TypeError):
        return {"error": "invalid task_id"}
    row = (await session.execute(
        tenant_filtered(select(DownloadTask).where(DownloadTask.id == tid),
                        DownloadTask, principal)
        .options(selectinload(DownloadTask.subtasks))
    )).scalar_one_or_none()
    if row is None:
        return {"error": "task not found"}
    if row.status not in ("succeeded", "failed", "cancelled"):
        return {"error": "task_not_terminal", "status": row.status}
    for sub in row.subtasks:
        await deref_subtask(session, sub.id)
    repo = row.repo_id
    await session.delete(row)
    await session.flush()
    return {"task_id": str(tid), "deleted": True, "repo_id": repo}


async def _retry(session: AsyncSession, principal: Principal, *,
                 task_id: str) -> dict:
    """Create a new download task using the parameters of an existing task.
    The original task is NOT modified — this is "re-download semantics"
    suitable for failed/cancelled tasks (or even successful ones, e.g. to
    pick up upstream HF revision changes). For state-machine retry that
    resets the same task's subtasks, a dedicated service is needed."""
    try:
        tid = uuid.UUID(str(task_id))
    except (ValueError, TypeError):
        return {"error": "invalid task_id"}
    original = (await session.execute(
        tenant_filtered(select(DownloadTask).where(DownloadTask.id == tid),
                        DownloadTask, principal)
    )).scalar_one_or_none()
    if original is None:
        return {"error": "task not found"}
    return await _create(
        session, principal,
        repo_id=original.repo_id, revision=original.revision,
        storage_id=original.storage_id,
        priority=original.priority or 1,
        source_strategy=original.source_strategy or "auto_balance",
    )


async def _create_local_user(session: AsyncSession, principal: Principal, *,
                              username: str, password: str,
                              tenant_id: int, role: str) -> dict:
    """Create a local username/password user. Requires system_admin role
    (enforced at the require_perm() boundary in the chat service)."""
    from fastapi import HTTPException
    from dlw.services.local_auth import create_user
    if principal.role != "system_admin":
        return {"error": "system_admin role required"}
    try:
        cred = await create_user(session, username, password,
                                  int(tenant_id), role)
    except HTTPException as e:
        return {"error": e.detail.get("code", "create_failed")
                if isinstance(e.detail, dict) else str(e.detail)}
    await session.flush()
    return {"user_id": cred.user_id, "username": cred.username,
            "tenant_id": cred.tenant_id, "role": cred.role,
            "must_change_password": cred.must_change_password}


async def _upgrade(session: AsyncSession, principal: Principal, *,
                   task_id: str, new_revision: str) -> dict:
    """Upgrade a task to a new revision by creating a NEW task at the new
    revision (same repo / storage / strategy). The scheduler's diff_and_dedup
    automatically inherits unchanged files from the original task, so only
    changed/new files are re-downloaded. The original task is unchanged."""
    try:
        tid = uuid.UUID(str(task_id))
    except (ValueError, TypeError):
        return {"error": "invalid task_id"}
    original = (await session.execute(
        tenant_filtered(select(DownloadTask).where(DownloadTask.id == tid),
                        DownloadTask, principal)
    )).scalar_one_or_none()
    if original is None:
        return {"error": "task not found"}
    if not new_revision or new_revision == original.revision:
        return {"error": "new_revision must differ from current revision"}
    return await _create(
        session, principal,
        repo_id=original.repo_id, revision=new_revision,
        storage_id=original.storage_id,
        priority=original.priority or 1,
        source_strategy=original.source_strategy or "auto_balance",
    )


async def _patch(session: AsyncSession, principal: Principal, *,
                 task_id: str, priority: int | None = None,
                 source_strategy: str | None = None,
                 source_blacklist: list | None = None) -> dict:
    """Patch mutable task fields. Rejects terminal-state tasks."""
    from dlw.services.task_patch import (
        InvalidPatch, TaskNotFound, TaskPatch, TaskTerminal, patch_task,
    )
    try:
        tid = uuid.UUID(str(task_id))
    except (ValueError, TypeError):
        return {"error": "invalid task_id"}
    # Tenant gate first (same pattern as _cancel)
    owned = await session.scalar(
        tenant_filtered(select(DownloadTask.id).where(DownloadTask.id == tid),
                        DownloadTask, principal))
    if owned is None:
        return {"error": "task not found"}
    if priority is None and source_strategy is None and source_blacklist is None:
        return {"error": "patch is empty — pass at least one field"}
    try:
        task = await patch_task(
            session, task_id=tid, tenant_id=principal.tenant_id,
            patch=TaskPatch(priority=priority,
                            source_strategy=source_strategy,
                            source_blacklist=source_blacklist))
    except TaskNotFound:
        return {"error": "task not found"}
    except TaskTerminal as e:
        return {"error": "task_terminal", "status": str(e)}
    except InvalidPatch as e:
        return {"error": "invalid_patch", "message": str(e)}
    return {"task_id": str(task.id), "priority": task.priority,
            "source_strategy": task.source_strategy,
            "source_blacklist": list(task.source_blacklist or [])}


async def _set_tenant_quota(session: AsyncSession, principal: Principal, *,
                             tenant_id: int,
                             quota_bytes_month: int | None = None,
                             quota_concurrent: int | None = None,
                             quota_storage_gb: int | None = None,
                             quota_ai_tokens_month: int | None = None) -> dict:
    """Set tenant quota limits. system_admin only; audit-logged."""
    from dlw.services.tenant_quota import (
        InvalidQuota, TenantNotFound, TenantQuotaPatch, set_tenant_quota,
    )
    if principal.role != "system_admin":
        return {"error": "system_admin role required"}
    try:
        tenant = await set_tenant_quota(
            session, tenant_id=int(tenant_id), actor_user_id=principal.user_id,
            patch=TenantQuotaPatch(
                quota_bytes_month=quota_bytes_month,
                quota_concurrent=quota_concurrent,
                quota_storage_gb=quota_storage_gb,
                quota_ai_tokens_month=quota_ai_tokens_month))
    except TenantNotFound:
        return {"error": "tenant not found"}
    except InvalidQuota as e:
        return {"error": "invalid_quota", "message": str(e)}
    return {"tenant_id": tenant.id,
            "quota_bytes_month": tenant.quota_bytes_month,
            "quota_concurrent": tenant.quota_concurrent,
            "quota_storage_gb": tenant.quota_storage_gb,
            "quota_ai_tokens_month": tenant.quota_ai_tokens_month}


async def _create_replication(session: AsyncSession, principal: Principal, *,
                               source_object_id: int,
                               target_storage_id: int) -> dict:
    """v2.1 SP6 — Create a cross-region replication job. system_admin only.

    Wraps services/replication.create_replication_job so all tenant + target
    validation runs naturally. The actual byte copy is picked up by the
    background worker_loop on the next poll tick."""
    from dlw.services.replication import (
        CreateJobRequest,
        DuplicateJob,
        InvalidTarget,
        ObjectNotFound,
        TargetNotFound,
        create_replication_job,
    )
    if principal.role != "system_admin":
        return {"error": "system_admin role required"}
    try:
        job = await create_replication_job(
            session, tenant_id=principal.tenant_id,
            actor_user_id=principal.user_id,
            req=CreateJobRequest(
                source_object_id=int(source_object_id),
                target_storage_id=int(target_storage_id)))
    except ObjectNotFound:
        return {"error": "source_object not found in your tenant"}
    except TargetNotFound:
        return {"error": "target_storage not found or not visible"}
    except InvalidTarget as e:
        return {"error": "invalid_target", "message": str(e)}
    except DuplicateJob as e:
        return {"error": "duplicate_job", "message": str(e)}
    await session.flush()
    return {"job_id": job.id, "status": job.status,
            "source_object_id": job.source_object_id,
            "target_storage_id": job.target_storage_id}


async def _reset_local_password(session: AsyncSession, principal: Principal, *,
                                 user_id: int, new_password: str) -> dict:
    """Reset a local user's password. system_admin only."""
    from fastapi import HTTPException
    from dlw.services.local_auth import reset_password
    if principal.role != "system_admin":
        return {"error": "system_admin role required"}
    try:
        await reset_password(session, int(user_id), new_password)
    except HTTPException as e:
        return {"error": e.detail.get("code", "reset_failed")
                if isinstance(e.detail, dict) else str(e.detail)}
    await session.flush()
    return {"user_id": int(user_id), "reset": True,
            "must_change_password": True}


WRITE_TOOLS: dict[str, WriteTool] = {
    "dlw_cancel_task": WriteTool(
        "dlw_cancel_task",
        "Cancel a running or pending download task. Requires user confirmation.",
        {"type": "object", "required": ["task_id"],
         "properties": {"task_id": {"type": "string"}}}, _cancel),
    "dlw_create_task": WriteTool(
        "dlw_create_task",
        "Create a new download task. Workflow: (1) optionally call "
        "search_huggingface_models / search_modelscope_models to find the "
        "repo_id, (2) optionally call dlw_list_storages to pick a "
        "storage_id (or omit to use the tenant default), (3) propose this "
        "tool — the user confirms. revision defaults to 'main' (HF "
        "resolves the sha). source_strategy defaults to 'auto_balance' "
        "which spreads chunks across all enabled sources. Consumes quota.",
        {"type": "object", "required": ["repo_id"],
         "properties": {"repo_id": {"type": "string",
                                    "description": "HF repo, e.g. 'meta-llama/Llama-3-8B'"},
                        "revision": {"type": "string",
                                     "description": "Branch/tag/sha (default 'main')"},
                        "storage_id": {"type": "integer",
                                       "description": "Omit to use tenant default"},
                        "priority": {"type": "integer", "default": 1},
                        "source_strategy": {"type": "string",
                                            "enum": ["auto_balance", "fastest_only",
                                                     "pin_huggingface", "pin_modelscope"]}}},
        _create),
    "dlw_delete_task": WriteTool(
        "dlw_delete_task",
        "Permanently delete a terminal task (succeeded / failed / cancelled). "
        "Frees storage and quota. Returns 'task_not_terminal' for in-progress "
        "tasks — cancel them first via dlw_cancel_task. Destructive: requires "
        "user confirmation.",
        {"type": "object", "required": ["task_id"],
         "properties": {"task_id": {"type": "string"}}}, _delete),
    "dlw_retry_task": WriteTool(
        "dlw_retry_task",
        "Re-download a task by creating a NEW task with the same repo_id / "
        "revision / storage / strategy. The original task is unchanged. "
        "Suitable for failed/cancelled tasks, or to refresh a succeeded one. "
        "Consumes fresh quota; requires user confirmation.",
        {"type": "object", "required": ["task_id"],
         "properties": {"task_id": {"type": "string"}}}, _retry),
    "dlw_create_local_user": WriteTool(
        "dlw_create_local_user",
        "Create a local username/password user. system_admin only. The new "
        "user starts with must_change_password=true. Requires confirmation.",
        {"type": "object",
         "required": ["username", "password", "tenant_id", "role"],
         "properties": {
             "username": {"type": "string", "minLength": 1, "maxLength": 64},
             "password": {"type": "string", "minLength": 8},
             "tenant_id": {"type": "integer"},
             "role": {"type": "string",
                      "enum": ["system_admin", "tenant_admin",
                               "tenant_operator", "tenant_viewer"]}}},
        _create_local_user),
    "dlw_reset_local_password": WriteTool(
        "dlw_reset_local_password",
        "Reset a local user's password to a new value. The user will be "
        "required to change it on next login (must_change_password=true). "
        "system_admin only; requires confirmation.",
        {"type": "object", "required": ["user_id", "new_password"],
         "properties": {"user_id": {"type": "integer"},
                        "new_password": {"type": "string", "minLength": 8}}},
        _reset_local_password),
    "dlw_upgrade_task": WriteTool(
        "dlw_upgrade_task",
        "Upgrade a task to a new revision. Creates a NEW task at "
        "new_revision (same repo / storage / strategy as the original). "
        "Unchanged files auto-inherit from the original task via the "
        "scheduler's diff_and_dedup — only changed/new files re-download. "
        "Original task is unchanged. Requires confirmation.",
        {"type": "object", "required": ["task_id", "new_revision"],
         "properties": {"task_id": {"type": "string"},
                        "new_revision": {"type": "string",
                                          "description": "Branch / tag / sha to upgrade to"}}},
        _upgrade),
    "dlw_patch_task": WriteTool(
        "dlw_patch_task",
        "Patch mutable fields of a non-terminal task: priority (0-10), "
        "source_strategy (auto_balance / fastest_only / pin_<source> / "
        "list:<csv>), source_blacklist (list of source ids to avoid). "
        "Rejects terminal tasks (succeeded / failed / cancelled). Pass at "
        "least one field. Requires confirmation.",
        {"type": "object", "required": ["task_id"],
         "properties": {"task_id": {"type": "string"},
                        "priority": {"type": "integer",
                                     "minimum": 0, "maximum": 10},
                        "source_strategy": {"type": "string"},
                        "source_blacklist": {"type": "array",
                                              "items": {"type": "string"}}}},
        _patch),
    "dlw_create_replication": WriteTool(
        "dlw_create_replication",
        "v2.1: Create a cross-region replication job that copies one "
        "storage_object to a different storage backend. system_admin "
        "only. The byte transfer runs in the background worker (poll "
        "tick is a few seconds) — this tool returns immediately after "
        "the job is queued. Requires confirmation. Common workflow: "
        "(1) dlw_list_storages to find source + target ids, (2) propose "
        "this tool, (3) user confirms.",
        {"type": "object",
         "required": ["source_object_id", "target_storage_id"],
         "properties": {
             "source_object_id": {"type": "integer",
                                   "description": "id of the existing storage_object to copy"},
             "target_storage_id": {"type": "integer",
                                    "description": "id of the destination storage backend"}}},
        _create_replication),
    "dlw_set_tenant_quota": WriteTool(
        "dlw_set_tenant_quota",
        "Set tenant quota limits (bytes/month, concurrent tasks, storage "
        "GB, AI tokens/month). system_admin only; pass at least one field; "
        "writes an audit log entry with before/after values. Requires "
        "confirmation.",
        {"type": "object", "required": ["tenant_id"],
         "properties": {"tenant_id": {"type": "integer"},
                        "quota_bytes_month": {"type": "integer", "minimum": 0},
                        "quota_concurrent": {"type": "integer", "minimum": 0},
                        "quota_storage_gb": {"type": "integer", "minimum": 0},
                        "quota_ai_tokens_month": {"type": "integer",
                                                   "minimum": 0}}},
        _set_tenant_quota),
}
