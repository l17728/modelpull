"""Subtasks API: POST /report (executor reports outcome).

Phase 2 W1: enforces X-Executor-Epoch header fence by looking up the subtask's
executor_id and verifying that executor's current epoch matches the header.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.api._recovery_barrier import require_not_recovering
from dlw.api.tasks import _session
from dlw.auth.executor_jwt_dep import require_executor_jwt
from dlw.db.models.executor import Executor
from dlw.db.models.task import FileSubTask
from dlw.schemas.subtask import SubTaskReport
from dlw.services.scheduler import complete_subtask

# v2.1 SP7 — file-extension → file_type bucket map. Coarse on purpose: the
# LP doesn't need MIME-level fidelity, just enough to separate
# safetensors-shard chunks from json/tokenizer chunks since their per-source
# throughput differs in the wild.
_FILE_TYPE_MAP = {
    ".safetensors": "safetensors", ".bin": "bin", ".pt": "bin",
    ".gguf": "gguf", ".json": "json", ".txt": "text", ".md": "text",
    ".yaml": "text", ".yml": "text",
}


def _file_type_from_name(name: str | None) -> str:
    if not name:
        return "other"
    for ext, kind in _FILE_TYPE_MAP.items():
        if name.lower().endswith(ext):
            return kind
    return "other"


router = APIRouter(prefix="/api/v1/subtasks", tags=["subtasks"])


@router.post("/{subtask_id}/report",
             dependencies=[Depends(require_not_recovering)])
async def post_report(
    subtask_id: uuid.UUID,
    body: SubTaskReport,
    # W6-D: use default=None so missing header raises 401 (not FastAPI auto-422)
    x_executor_epoch: int | None = Header(default=None, alias="X-Executor-Epoch"),
    auth_ex: Executor = Depends(require_executor_jwt),
    session: AsyncSession = Depends(_session),
) -> dict[str, str]:
    """W3a: mTLS + JWT auth. The reporting executor must be the one the
    subtask is assigned to (confused-deputy guard) and its epoch must match."""
    if x_executor_epoch is None:
        raise HTTPException(
            status_code=401,
            detail="missing X-Executor-Epoch header",
        )
    sub = await session.get(FileSubTask, subtask_id)
    if sub is None:
        raise HTTPException(status_code=404, detail=f"subtask {subtask_id} not found")
    if sub.executor_id is None:
        raise HTTPException(
            status_code=409, detail=f"subtask {subtask_id} not assigned"
        )
    # W3a confused-deputy guard: the mTLS-authenticated executor MUST be the
    # one the subtask is claimed by.
    if sub.executor_id != auth_ex.id:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "EXECUTOR_ID_MISMATCH",
                "subtask_executor": sub.executor_id,
                "authenticated": auth_ex.id,
            },
        )
    # Phase 2 W1 fence: verify epoch against the authenticated executor row.
    if auth_ex.epoch != x_executor_epoch:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "EPOCH_MISMATCH",
                "expected": auth_ex.epoch,
                "got": x_executor_epoch,
            },
        )

    try:
        sub_done, parent = await complete_subtask(
            session, subtask_id,
            final_status=body.status,
            actual_sha256=body.actual_sha256,
            bytes_downloaded=body.bytes_downloaded,
            error=body.error,
            assignment_token=body.assignment_token,
            executor_epoch=x_executor_epoch,    # forward to fence gate
            s3_key=body.s3_key,
        )
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    await session.commit()

    # v2.1 SP7 — record a per-subtask throughput sample on success. This
    # is the chunk-complete signal for the Sprint 8 LP solver. Non-blocking:
    # record_sample just appends to an in-memory deque — the flush_loop in
    # main.py lifespan drains in batches.
    if sub_done.status == "succeeded":
        from datetime import UTC, datetime
        from dlw.services.throughput_sampler import Sample, record_sample
        assigned_at = sub_done.assigned_at
        completed_at = sub_done.completed_at or datetime.now(UTC)
        if assigned_at is not None:
            duration_ms = int(
                (completed_at - assigned_at).total_seconds() * 1000)
            if duration_ms > 0:
                record_sample(Sample(
                    executor_id=auth_ex.id,
                    source_id=sub_done.source_id or "unknown",
                    file_type=_file_type_from_name(sub_done.filename),
                    bytes_transferred=int(body.bytes_downloaded),
                    duration_ms=duration_ms,
                    tenant_id=sub_done.tenant_id))

    return {"subtask_status": sub_done.status, "task_status": parent.status}
