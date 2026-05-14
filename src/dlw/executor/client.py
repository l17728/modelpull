"""HTTP client wrapping the controller's executor + subtask endpoints.

Phase 2 W3a: auth is mTLS (client cert) + executor JWT + (heartbeat only) HMAC.
The client is driven by an AuthState (mutable — the runner's renew loop swaps
it in place via update_auth). Each request builds an httpx.AsyncClient with
verify=<ca-chain> + cert=(<client-cert>, <client-key>) and an
Authorization: Bearer <jwt> header. heartbeat additionally signs the body
with HMAC-SHA256 over the executor's hmac_seed.

The W1 /join + epoch-persistence logic is gone — registration happens via
dlw.executor.auth_lifecycle, and the epoch lives on the AuthState.
"""
from __future__ import annotations

import secrets
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Self

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from dlw.auth.hmac_nonce import compute_hmac
from dlw.executor.auth_lifecycle import AuthState


_retry = retry(
    retry=retry_if_exception_type(
        (httpx.HTTPStatusError, httpx.TimeoutException, httpx.NetworkError)
    ),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, max=4.0),
    reraise=True,
)


class ControllerClient:
    """Async HTTP client for controller endpoints (executor side).

    Auth is carried by an AuthState. The runner's renew loop updates it in
    place via update_auth(); each request reads the current state.
    """

    def __init__(
        self,
        base_url: str,
        auth_state: AuthState | None = None,
        timeout_seconds: float = 30.0,
        _transport: httpx.AsyncBaseTransport | None = None,
        _extra_test_headers: dict[str, str] | None = None,
    ) -> None:
        # auth_state may be None at construction (cli.py builds the client
        # before run() does load_or_register); the runner calls update_auth()
        # before any request is made. Requests crash if auth is still None.
        #
        # _extra_test_headers is a test-only seam: when an ASGI transport is
        # injected (no real TLS), tests pass {"X-Client-Cert-PEM": ...} so the
        # controller's trusted-proxy mTLS path can authenticate. Production
        # leaves it None and relies on real mTLS via cert=(...).
        self._base_url = base_url.rstrip("/")
        self._auth = auth_state
        self._timeout = timeout_seconds
        self._transport = _transport
        self._extra_test_headers = _extra_test_headers or {}

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        # Per-request clients are created + closed inside each call; nothing to
        # close here. Kept for API compatibility with the W1 context-manager use.
        return None

    def update_auth(self, new_state: AuthState) -> None:
        """Swap the AuthState (called by the runner's renew loop)."""
        self._auth = new_state

    def current_epoch(self) -> int:
        """Current executor epoch from the AuthState."""
        return self._auth.epoch

    def _make_client(self) -> httpx.AsyncClient:
        """Build a per-request httpx client. When a test transport is injected,
        cert/verify are skipped (MockTransport short-circuits the network)."""
        if self._transport is not None:
            return httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout,
                transport=self._transport,
            )
        cert_dir = self._auth.cert_dir
        return httpx.AsyncClient(
            base_url=self._base_url, timeout=self._timeout,
            verify=str(cert_dir / "ca-chain.pem"),
            cert=(str(cert_dir / "client-cert.pem"),
                  str(cert_dir / "client-key.pem")),
        )

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._auth.jwt}",
            "X-Executor-Epoch": str(self._auth.epoch),
            **self._extra_test_headers,
        }

    async def _post_json(
        self,
        path: str,
        json_body: dict[str, Any] | None,
        extra_headers: dict[str, str],
    ) -> dict[str, Any]:
        @_retry
        async def _do() -> httpx.Response:
            async with self._make_client() as client:
                r = await client.post(path, json=json_body, headers=extra_headers)
                if 500 <= r.status_code < 600:
                    r.raise_for_status()
                return r

        r = await _do()
        r.raise_for_status()
        return r.json()

    async def _post_content(
        self,
        path: str,
        content: bytes,
        extra_headers: dict[str, str],
    ) -> dict[str, Any]:
        @_retry
        async def _do() -> httpx.Response:
            async with self._make_client() as client:
                r = await client.post(path, content=content, headers=extra_headers)
                if 500 <= r.status_code < 600:
                    r.raise_for_status()
                return r

        r = await _do()
        r.raise_for_status()
        return r.json()

    async def heartbeat(
        self,
        *,
        executor_id: str,
        health_score: int,
        parts_dir_bytes: int,
        disk_free_gb: int | None = None,
    ) -> dict[str, Any]:
        """POST /heartbeat with mTLS + JWT + HMAC. The body is sent as raw
        content (not json=) so the HMAC signature covers the exact bytes."""
        import json as _json
        body_dict: dict[str, Any] = {
            "health_score": health_score,
            "parts_dir_bytes": parts_dir_bytes,
        }
        if disk_free_gb is not None:
            body_dict["disk_free_gb"] = disk_free_gb
        body = _json.dumps(body_dict).encode("utf-8")
        ts = int(time.time())
        nonce = secrets.token_hex(16)
        sig = compute_hmac(self._auth.hmac_seed, ts=ts, nonce=nonce, body=body)
        headers = {
            **self._auth_headers(),
            "Content-Type": "application/json",
            "X-HMAC-Timestamp": str(ts),
            "X-HMAC-Nonce": nonce,
            "X-HMAC-Signature": sig,
        }
        return await self._post_content(
            f"/api/v1/executors/{executor_id}/heartbeat", body, headers,
        )

    async def poll(self, *, executor_id: str) -> dict[str, Any]:
        return await self._post_json(
            f"/api/v1/executors/{executor_id}/poll", None, self._auth_headers(),
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
        return await self._post_json(
            f"/api/v1/subtasks/{subtask_id}/report", body, self._auth_headers(),
        )

    @asynccontextmanager
    async def stream_hf(
        self,
        *,
        subtask_id: uuid.UUID,
        assignment_token: uuid.UUID,
        range_header: str | None = None,
    ) -> AsyncIterator[httpx.Response]:
        """W3b: stream a file from HF via the controller reverse-proxy. Yields
        the httpx streaming Response — callers consume resp.aiter_bytes() and
        check resp.status_code, exactly as they did with a direct HF GET.

        Unlike heartbeat/poll/report, this does NOT call raise_for_status() —
        callers MUST inspect resp.status_code themselves (the downloaders do)."""
        headers = {
            **self._auth_headers(),
            "X-Assignment-Token": str(assignment_token),
        }
        if range_header:
            headers["Range"] = range_header
        async with self._make_client() as client:
            async with client.stream(
                "GET",
                f"/api/v1/hf-proxy/subtask/{subtask_id}",
                headers=headers,
            ) as resp:
                yield resp
