"""Generalized multi-source reverse-proxy (Phase 3 SP2). Mirrors the W3b
hf_proxy ownership chain; routes each subtask/chunk to its assigned
SourceDriver and injects that source's controller-side credential. The
source token NEVER leaves the controller (INVARIANT 2)."""
from __future__ import annotations

import uuid

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.api.tasks import _session
from dlw.auth.executor_jwt_dep import require_executor_jwt
from dlw.config import get_settings
from dlw.db.models.executor import Executor
from dlw.db.models.source import SubtaskChunk
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.sources.base import SourceFile

router = APIRouter(prefix="/api/v1/source-proxy", tags=["executors"])

_HDR_ALLOW = frozenset({
    "content-length", "content-range", "content-type",
    "accept-ranges", "etag",
})


def _make_source_client(timeout_seconds: int) -> httpx.AsyncClient:
    """Test seam — monkeypatched to inject httpx.MockTransport."""
    return httpx.AsyncClient(follow_redirects=True, timeout=timeout_seconds)


@router.get("/subtask/{subtask_id}")
async def source_proxy_subtask(
    subtask_id: uuid.UUID,
    request: Request,
    x_assignment_token: str = Header(..., alias="X-Assignment-Token"),
    auth_ex: Executor = Depends(require_executor_jwt),
    session: AsyncSession = Depends(_session),
) -> StreamingResponse:
    sub = await session.get(FileSubTask, subtask_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="subtask not found")
    if sub.executor_id != auth_ex.id:
        raise HTTPException(
            status_code=403,
            detail={"code": "NOT_YOUR_SUBTASK",
                    "subtask_executor": sub.executor_id,
                    "authenticated": auth_ex.id},
        )
    if sub.assignment_token is None or str(sub.assignment_token) != x_assignment_token:
        raise HTTPException(
            status_code=409, detail={"code": "STALE_ASSIGNMENT"},
        )
    if sub.executor_epoch != auth_ex.epoch:
        raise HTTPException(
            status_code=409,
            detail={"code": "EPOCH_MISMATCH",
                    "expected": sub.executor_epoch, "got": auth_ex.epoch},
        )

    task = await session.get(DownloadTask, sub.task_id)
    if task is None:
        raise HTTPException(status_code=500, detail="parent task missing")

    settings = get_settings()
    range_header = request.headers.get("Range")

    source_id = sub.source_id
    if sub.is_chunked and range_header and range_header.startswith("bytes="):
        start = int(range_header.split("=", 1)[1].split("-", 1)[0])
        chunk = await session.scalar(select(SubtaskChunk).where(
            SubtaskChunk.subtask_id == sub.id,
            SubtaskChunk.byte_start <= start,
            SubtaskChunk.byte_end >= start))
        if chunk is not None:
            source_id = chunk.source_id
    if source_id is None:
        raise HTTPException(status_code=409, detail={"code": "SOURCE_UNASSIGNED"})

    registry = request.app.state.source_registry
    drv = registry.get(source_id)
    if drv is None:
        raise HTTPException(status_code=502, detail={"code": "SOURCE_UNAVAILABLE"})

    src_file = SourceFile(filename=sub.filename, size=sub.file_size,
                          sha256=sub.expected_sha256,
                          download_ref=f"{task.repo_id}/resolve/"
                                       f"{task.revision}/{sub.filename}")
    url = drv.download_url(src_file)
    tok = drv.auth_token(settings.hf_token)
    headers: dict[str, str] = {}
    if tok.scheme == "bearer" and tok.value:
        headers["Authorization"] = f"Bearer {tok.value}"
    if range_header:
        headers["Range"] = range_header

    client = _make_source_client(settings.hf_proxy_timeout_seconds)
    req = client.build_request("GET", url, headers=headers)
    try:
        resp = await client.send(req, stream=True)
    except (httpx.TimeoutException, httpx.NetworkError) as e:
        await client.aclose()
        raise HTTPException(
            status_code=503, detail=f"source unreachable: {e}",
        ) from e
    except BaseException:
        await client.aclose()
        raise

    fwd = {k: v for k, v in resp.headers.items()
           if k.lower() in _HDR_ALLOW}

    async def _body():
        try:
            async for chunk in resp.aiter_bytes(64 * 1024):
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(_body(), status_code=resp.status_code,
                             headers=fwd)
