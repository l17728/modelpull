"""Patch mutable fields on a DownloadTask.

Mutable in any non-terminal state:
  - priority         (scheduler reads it on next plan round)
  - source_strategy  (chunk-level scheduler reads it each round)
  - source_blacklist (chunk-level scheduler reads it each round)

Terminal tasks (succeeded / failed / cancelled) are immutable — return
TaskTerminal so callers can map to HTTP 409. Caller commits."""
from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.task import DownloadTask

logger = logging.getLogger(__name__)

_TERMINAL = {"succeeded", "failed", "cancelled"}
_VALID_STRATEGIES = {
    "auto_balance", "fastest_only",
}  # pin_<source> and list:<csv> are also valid but validated structurally


class TaskNotFound(LookupError):
    """No task with that id (or not in the caller's tenant)."""


class TaskTerminal(ValueError):
    """Task is in a terminal status and cannot be patched."""


class InvalidPatch(ValueError):
    """Patch payload failed structural validation."""


@dataclass(frozen=True)
class TaskPatch:
    """Optional fields. None means "leave unchanged"."""
    priority: int | None = None
    source_strategy: str | None = None
    source_blacklist: Sequence[str] | None = None


def _validate_strategy(s: str) -> None:
    if s in _VALID_STRATEGIES:
        return
    if s.startswith("pin_") and len(s) > 4:
        return
    if s.startswith("list:") and len(s) > 5:
        return
    raise InvalidPatch(f"source_strategy {s!r} not recognized")


async def patch_task(
    session: AsyncSession, *, task_id, tenant_id: int, patch: TaskPatch,
) -> DownloadTask:
    """Apply patch to the named task (tenant-scoped). Raises TaskNotFound /
    TaskTerminal / InvalidPatch. Returns the mutated task (uncommitted)."""
    task = (await session.execute(
        select(DownloadTask).where(
            DownloadTask.id == task_id,
            DownloadTask.tenant_id == tenant_id,
        ).with_for_update()
    )).scalar_one_or_none()
    if task is None:
        logger.warning(
            "patch_task: task %s not found in tenant %d", task_id, tenant_id)
        raise TaskNotFound(str(task_id))
    if task.status in _TERMINAL:
        logger.warning(
            "patch_task: rejected for terminal task %s (status=%s)",
            task_id, task.status)
        raise TaskTerminal(task.status)

    changes: dict[str, tuple] = {}
    if patch.priority is not None:
        if not (0 <= int(patch.priority) <= 10):
            logger.warning("patch_task: invalid priority %r for %s",
                           patch.priority, task_id)
            raise InvalidPatch("priority must be 0..10")
        old = task.priority
        new = int(patch.priority)
        if old != new:
            task.priority = new
            changes["priority"] = (old, new)
    if patch.source_strategy is not None:
        _validate_strategy(patch.source_strategy)
        old = task.source_strategy
        if old != patch.source_strategy:
            task.source_strategy = patch.source_strategy
            changes["source_strategy"] = (old, patch.source_strategy)
    if patch.source_blacklist is not None:
        if not isinstance(patch.source_blacklist, (list, tuple)):
            logger.warning(
                "patch_task: source_blacklist not a list for %s", task_id)
            raise InvalidPatch("source_blacklist must be a list of source ids")
        new_bl = [str(s) for s in patch.source_blacklist]
        old_bl = list(task.source_blacklist or [])
        if old_bl != new_bl:
            task.source_blacklist = new_bl
            changes["source_blacklist"] = (old_bl, new_bl)

    if changes:
        logger.info(
            "patch_task: task=%s tenant=%d changes=%s",
            task_id, tenant_id,
            {k: {"before": v[0], "after": v[1]} for k, v in changes.items()})
    else:
        logger.debug("patch_task: task=%s no-op (all fields unchanged)", task_id)
    await session.flush()
    return task
