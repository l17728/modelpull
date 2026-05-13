"""HTTP client wrapping the controller's executor + subtask endpoints.

Phase 2 W1 additions:
  - Persists the executor's current epoch from /join response.
  - Attaches X-Executor-Epoch header on heartbeat / poll / report.
  - Caller (runner) should observe `current_epoch()` and react to 401
    EPOCH_MISMATCH by calling join() again.
"""
from __future__ import annotations

import uuid
from typing import Any, Self

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


_retry = retry(
    retry=retry_if_exception_type(
        (httpx.HTTPStatusError, httpx.TimeoutException, httpx.NetworkError)
    ),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, max=4.0),
    reraise=True,
)


class ControllerClient:
    """Async HTTP client for controller endpoints (executor side)."""

    def __init__(
        self,
        base_url: str,
        bearer_token: str,
        timeout_seconds: float = 30.0,
        _transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {bearer_token}"}
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=timeout_seconds,
            transport=_transport,
        )
        self._epoch: int | None = None        # P2-W1

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self._client.aclose()

    def current_epoch(self) -> int | None:
        """Returns the most recent epoch from /join, or None if not joined yet."""
        return self._epoch

    def _epoch_headers(self) -> dict[str, str]:
        if self._epoch is None:
            return {}
        return {"X-Executor-Epoch": str(self._epoch)}

    async def _post(
        self,
        path: str,
        json_body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = {**(extra_headers or {})}

        @_retry
        async def _do() -> httpx.Response:
            r = await self._client.post(path, json=json_body, headers=headers)
            if 500 <= r.status_code < 600:
                r.raise_for_status()
            return r

        r = await _do()
        r.raise_for_status()
        return r.json()

    async def join(
        self, *, executor_id: str, host_id: str, capabilities: dict[str, Any]
    ) -> dict[str, Any]:
        body = await self._post("/api/v1/executors/join", {
            "id": executor_id, "host_id": host_id, "capabilities": capabilities,
        })
        epoch = body.get("epoch")
        if isinstance(epoch, int):
            self._epoch = epoch
        return body

    async def heartbeat(
        self,
        *,
        executor_id: str,
        health_score: int,
        parts_dir_bytes: int,
        disk_free_gb: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "health_score": health_score,
            "parts_dir_bytes": parts_dir_bytes,
        }
        if disk_free_gb is not None:
            body["disk_free_gb"] = disk_free_gb
        return await self._post(
            f"/api/v1/executors/{executor_id}/heartbeat",
            body,
            extra_headers=self._epoch_headers(),
        )

    async def poll(self, *, executor_id: str) -> dict[str, Any]:
        return await self._post(
            f"/api/v1/executors/{executor_id}/poll",
            extra_headers=self._epoch_headers(),
        )

    async def report(
        self,
        *,
        subtask_id: uuid.UUID,
        status: str,
        assignment_token: uuid.UUID | None,
        actual_sha256: str | None,
        bytes_downloaded: int,
        error: str | None = None,
        s3_key: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "status": status,
            "bytes_downloaded": bytes_downloaded,
        }
        if assignment_token is not None:
            body["assignment_token"] = str(assignment_token)
        if actual_sha256 is not None:
            body["actual_sha256"] = actual_sha256
        if error is not None:
            body["error"] = error
        if s3_key is not None:
            body["s3_key"] = s3_key
        return await self._post(
            f"/api/v1/subtasks/{subtask_id}/report",
            body,
            extra_headers=self._epoch_headers(),
        )
