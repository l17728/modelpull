"""Patch mutable fields on a DownloadTask.

Mutable in any non-terminal state:
  - priority         (scheduler reads it on next plan round)
  - source_strategy  (chunk-level scheduler reads it each round)
  - source_blacklist (chunk-level scheduler reads it each round)

Terminal tasks (succeeded / failed / cancelled) are immutable — return
TaskTerminal so callers can map to HTTP 409. Caller commits."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.task import DownloadTask

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
        raise TaskNotFound(str(task_id))
    if task.status in _TERMINAL:
        raise TaskTerminal(task.status)

    if patch.priority is not None:
        if not (0 <= int(patch.priority) <= 10):
            raise InvalidPatch("priority must be 0..10")
        task.priority = int(patch.priority)
    if patch.source_strategy is not None:
        _validate_strategy(patch.source_strategy)
        task.source_strategy = patch.source_strategy
    if patch.source_blacklist is not None:
        if not isinstance(patch.source_blacklist, (list, tuple)):
            raise InvalidPatch("source_blacklist must be a list of source ids")
        task.source_blacklist = [str(s) for s in patch.source_blacklist]

    await session.flush()
    return task
