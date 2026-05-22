"""Asynchronous dlw SDK client — mirrors client.py."""
from __future__ import annotations

from typing import Any

import httpx

from dlw.sdk._config import resolve
from dlw.sdk._http import raise_for_status
from dlw.sdk.errors import Timeout
from dlw.sdk.models import TERMINAL, DownloadTask


class AsyncQuotaAPI:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._h = http

    async def current(self) -> dict:
        r = await self._h.get("/api/v1/quota/current")
        raise_for_status(r)
        return r.json()


class AsyncExecutorsAPI:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._h = http

    async def list(self, *, status: str | None = None) -> dict:
        params = {"status": status} if status else None
        r = await self._h.get("/api/v1/executors", params=params)
        raise_for_status(r)
        return r.json()


class AsyncAuditAPI:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._h = http

    async def search(self, *, action: str | None = None,
                     actor_user_id: int | None = None,
                     from_: str | None = None,
                     to: str | None = None, cursor: str | None = None,
                     limit: int = 50) -> dict:
        params: dict[str, Any] = {"limit": limit}
        if action is not None:
            params["action"] = action
        if actor_user_id is not None:
            params["actor_user_id"] = actor_user_id
        if from_ is not None:
            params["from"] = from_
        if to is not None:
            params["to"] = to
        if cursor is not None:
            params["cursor"] = cursor
        r = await self._h.get("/api/v1/audit/log", params=params)
        raise_for_status(r)
        return r.json()


class AsyncDownloadTask(DownloadTask):
    async def refresh(self) -> "AsyncDownloadTask":  # type: ignore[override]
        if self._api is None:
            raise RuntimeError("detached AsyncDownloadTask has no client")
        return await self._api.get(self.id)

    async def wait(self, timeout: float | None = None,  # type: ignore[override]
                   on_progress=None,
                   poll_interval: float = 5.0) -> "AsyncDownloadTask":
        import asyncio
        import time
        start = time.monotonic()
        cur: AsyncDownloadTask = self
        while cur.status not in TERMINAL:
            if timeout is not None and time.monotonic() - start > timeout:
                raise Timeout(
                    f"task {self.id} not terminal after {timeout}s")
            await asyncio.sleep(poll_interval)
            cur = await self._api.get(self.id)
            if on_progress is not None:
                on_progress(cur)
        return cur


class AsyncTasksAPI:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._h = http

    async def submit(self, repo_id: str, revision: str, *, storage_id: int,
                      priority: int = 1,
                      source_strategy: str = "auto_balance",
                      source_blacklist: list[str] | None = None,
                      trust_non_hf_sha256: bool = False,
                      upgrade_from_revision: str | None = None,
                      path_template: str = "{tenant}/{repo_id}/{revision}",
                      ) -> AsyncDownloadTask:
        body: dict[str, Any] = {
            "repo_id": repo_id, "revision": revision,
            "storage_id": storage_id, "priority": priority,
            "source_strategy": source_strategy,
            "source_blacklist": source_blacklist or [],
            "trust_non_hf_sha256": trust_non_hf_sha256,
            "path_template": path_template,
        }
        if upgrade_from_revision is not None:
            body["upgrade_from_revision"] = upgrade_from_revision
        r = await self._h.post("/api/v1/tasks", json=body)
        raise_for_status(r)
        return AsyncDownloadTask.from_api(r.json(), api=self)

    async def get(self, task_id: str) -> AsyncDownloadTask:
        r = await self._h.get(f"/api/v1/tasks/{task_id}")
        raise_for_status(r)
        return AsyncDownloadTask.from_api(r.json(), api=self)

    async def list(self, *, status: str | list[str] | None = None,
                   limit: int = 50) -> list[AsyncDownloadTask]:
        r = await self._h.get("/api/v1/tasks")
        raise_for_status(r)
        items = r.json().get("items", [])
        if status is not None:
            want = {status} if isinstance(status, str) else set(status)
            items = [i for i in items if i.get("status") in want]
        return [AsyncDownloadTask.from_api(i, api=self)
                for i in items[:limit]]

    async def cancel(self, task_id: str, reason: str | None = None) -> None:
        body = {"reason": reason} if reason else {}
        r = await self._h.post(f"/api/v1/tasks/{task_id}/cancel", json=body)
        raise_for_status(r)

    async def delete(self, task_id: str) -> None:
        r = await self._h.request("DELETE", f"/api/v1/tasks/{task_id}")
        raise_for_status(r)

    async def events(self, task_id: str, *, limit: int = 50,
                     cursor: str | None = None) -> dict:
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        r = await self._h.get(f"/api/v1/tasks/{task_id}/events", params=params)
        raise_for_status(r)
        return r.json()

    def events_stream(self, task_id: str, *, max_ticks: int | None = None):
        """SSE seam for `dlw events --follow`. Returns the async httpx streaming
        context manager (caller iterates `.aiter_lines()`). `max_ticks` bounds
        the stream for tests; production passes None (runs until disconnect)."""
        params = {"max_ticks": max_ticks} if max_ticks is not None else None
        return self._h.stream(
            "GET", f"/api/v1/tasks/{task_id}/events/stream", params=params)

    def task_stream(self, task_id: str, *, max_ticks: int | None = None,
                    timeout=None):
        """SSE seam for `dlw watch`: streams TaskDetail from /tasks/{id}/stream
        (self-terminates on terminal status). `timeout` (httpx read-timeout)
        makes a STALLED stream raise rather than hang."""
        params = {"max_ticks": max_ticks} if max_ticks is not None else None
        kw = {"params": params}
        if timeout is not None:
            kw["timeout"] = timeout
        return self._h.stream("GET", f"/api/v1/tasks/{task_id}/stream", **kw)


class AsyncClient:
    def __init__(self, server: str | None = None, token: str | None = None,
                 *, config_path: str | None = None, timeout: float = 30.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        r = resolve(server=server, token=token, config_path=config_path)
        self._http = httpx.AsyncClient(
            base_url=r.server, timeout=timeout,
            headers={"Authorization": f"Bearer {r.token}"},
            transport=transport)
        self.tasks = AsyncTasksAPI(self._http)
        self.quota = AsyncQuotaAPI(self._http)
        self.executors = AsyncExecutorsAPI(self._http)
        self.audit = AsyncAuditAPI(self._http)

    async def me(self) -> dict:
        r = await self._http.get("/api/v1/auth/me")
        raise_for_status(r)
        return r.json()

    @classmethod
    def from_env(cls, **kw: Any) -> "AsyncClient":
        return cls(**kw)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "AsyncClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
