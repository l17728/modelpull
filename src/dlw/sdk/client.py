"""Synchronous dlw SDK client."""
from __future__ import annotations

from typing import Any

import httpx

from dlw.sdk._config import resolve
from dlw.sdk._http import raise_for_status
from dlw.sdk.models import DownloadTask


class QuotaAPI:
    def __init__(self, http: httpx.Client) -> None:
        self._h = http

    def current(self) -> dict:
        r = self._h.get("/api/v1/quota/current")
        raise_for_status(r)
        return r.json()


class ExecutorsAPI:
    def __init__(self, http: httpx.Client) -> None:
        self._h = http

    def list(self, *, status: str | None = None) -> dict:
        params = {"status": status} if status else None
        r = self._h.get("/api/v1/executors", params=params)
        raise_for_status(r)
        return r.json()


class AuditAPI:
    def __init__(self, http: httpx.Client) -> None:
        self._h = http

    def search(self, *, action: str | None = None,
               actor_user_id: int | None = None, from_: str | None = None,
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
        r = self._h.get("/api/v1/audit/log", params=params)
        raise_for_status(r)
        return r.json()


class TasksAPI:
    def __init__(self, http: httpx.Client) -> None:
        self._h = http

    def submit(self, repo_id: str, revision: str, *, storage_id: int,
               priority: int = 1, source_strategy: str = "auto_balance",
               source_blacklist: list[str] | None = None,
               trust_non_hf_sha256: bool = False,
               upgrade_from_revision: str | None = None,
               path_template: str = "{tenant}/{repo_id}/{revision}",
               ) -> DownloadTask:
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
        r = self._h.post("/api/v1/tasks", json=body)
        raise_for_status(r)
        return DownloadTask.from_api(r.json(), api=self)

    def get(self, task_id: str) -> DownloadTask:
        r = self._h.get(f"/api/v1/tasks/{task_id}")
        raise_for_status(r)
        return DownloadTask.from_api(r.json(), api=self)

    def list(self, *, status: str | list[str] | None = None,
             limit: int = 50) -> list[DownloadTask]:
        r = self._h.get("/api/v1/tasks")
        raise_for_status(r)
        items = r.json().get("items", [])
        if status is not None:
            want = {status} if isinstance(status, str) else set(status)
            items = [i for i in items if i.get("status") in want]
        return [DownloadTask.from_api(i, api=self) for i in items[:limit]]

    def cancel(self, task_id: str, reason: str | None = None) -> None:
        body = {"reason": reason} if reason else {}
        r = self._h.post(f"/api/v1/tasks/{task_id}/cancel", json=body)
        raise_for_status(r)

    def delete(self, task_id: str) -> None:
        r = self._h.request("DELETE", f"/api/v1/tasks/{task_id}")
        raise_for_status(r)

    def events(self, task_id: str, *, limit: int = 50,
               cursor: str | None = None) -> dict:
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        r = self._h.get(f"/api/v1/tasks/{task_id}/events", params=params)
        raise_for_status(r)
        return r.json()

    def events_stream(self, task_id: str, *, max_ticks: int | None = None):
        """SSE seam for `dlw events --follow`. Returns the httpx streaming
        context manager (caller iterates `.iter_lines()`). `max_ticks` bounds
        the stream for tests; production passes None (runs until disconnect)."""
        params = {"max_ticks": max_ticks} if max_ticks is not None else None
        return self._h.stream(
            "GET", f"/api/v1/tasks/{task_id}/events/stream", params=params)


class Client:
    def __init__(self, server: str | None = None, token: str | None = None,
                 *, config_path: str | None = None, timeout: float = 30.0,
                 transport: httpx.BaseTransport | None = None) -> None:
        r = resolve(server=server, token=token, config_path=config_path)
        self._http = httpx.Client(
            base_url=r.server, timeout=timeout,
            headers={"Authorization": f"Bearer {r.token}"},
            transport=transport)
        self.tasks = TasksAPI(self._http)
        self.quota = QuotaAPI(self._http)
        self.executors = ExecutorsAPI(self._http)
        self.audit = AuditAPI(self._http)

    def me(self) -> dict:
        r = self._http.get("/api/v1/auth/me")
        raise_for_status(r)
        return r.json()

    @classmethod
    def from_env(cls, **kw: Any) -> "Client":
        return cls(**kw)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
