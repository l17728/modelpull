"""HF reverse-proxy — controller-side, injects the tenant HF token (SEC-02).

The executor never holds the HF token (INVARIANT 2). It calls this proxy keyed
by subtask_id; the controller verifies ownership (assignment_token + epoch fence
+ confused-deputy guard — added in Task 3), reconstructs the HF URL from the
subtask row, injects Settings.hf_token, follows HF's 302->CDN redirect
server-side, and streams the bytes back.
"""
from __future__ import annotations

import uuid

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.api.tasks import _session
from dlw.auth.executor_jwt_dep import require_executor_jwt
from dlw.config import get_settings
from dlw.db.models.executor import Executor
from dlw.db.models.task import DownloadTask, FileSubTask

router = APIRouter(prefix="/api/v1/hf-proxy", tags=["executors"])

_HF_HEADER_ALLOWLIST = frozenset({
    "content-length", "content-range", "content-type",
    "accept-ranges", "etag",
})


def _make_hf_client(timeout_seconds: int) -> httpx.AsyncClient:
    """Test seam — monkeypatched in tests to inject an httpx.MockTransport."""
    return httpx.AsyncClient(follow_redirects=True, timeout=timeout_seconds)


@router.get("/subtask/{subtask_id}")
async def hf_proxy_subtask(
    subtask_id: uuid.UUID,
    request: Request,
    x_assignment_token: str = Header(..., alias="X-Assignment-Token"),
    auth_ex: Executor = Depends(require_executor_jwt),
    session: AsyncSession = Depends(_session),
) -> StreamingResponse:
    sub = await session.get(FileSubTask, subtask_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="subtask not found")

    task = await session.get(DownloadTask, sub.task_id)
    if task is None:                       # FK guarantees this won't happen
        raise HTTPException(status_code=500, detail="parent task missing")

    settings = get_settings()
    hf_url = (f"{settings.hf_endpoint.rstrip('/')}/{task.repo_id}"
              f"/resolve/{task.revision}/{sub.filename}")

    hf_headers: dict[str, str] = {}
    if settings.hf_token:
        hf_headers["Authorization"] = f"Bearer {settings.hf_token}"
    range_header = request.headers.get("Range")
    if range_header:
        hf_headers["Range"] = range_header

    hf_client = _make_hf_client(settings.hf_proxy_timeout_seconds)
    hf_req = hf_client.build_request("GET", hf_url, headers=hf_headers)
    hf_resp = await hf_client.send(hf_req, stream=True)

    fwd = {
        k: v for k, v in hf_resp.headers.items()
        if k.lower() in _HF_HEADER_ALLOWLIST
    }

    async def _body():
        try:
            async for chunk in hf_resp.aiter_bytes(64 * 1024):
                yield chunk
        finally:
            await hf_resp.aclose()
            await hf_client.aclose()

    return StreamingResponse(
        _body(), status_code=hf_resp.status_code, headers=fwd,
    )
