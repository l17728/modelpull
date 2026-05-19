"""Public DownloadTask wrapper + terminal-status set."""
from __future__ import annotations

from typing import Any

TERMINAL = {"succeeded", "failed", "cancelled"}


class DownloadTask:
    """Wraps a TaskRead/TaskDetail JSON object. `_api` (a TasksAPI or
    AsyncTasksAPI) backs refresh()/wait(); None for detached parsing."""

    def __init__(self, *, id: str, repo_id: str, revision: str, status: str,
                 priority: int, created_at: str, completed_at: str | None,
                 error_message: str | None, subtasks: list[dict],
                 raw: dict, api: Any = None) -> None:
        self.id = id
        self.repo_id = repo_id
        self.revision = revision
        self.status = status
        self.priority = priority
        self.created_at = created_at
        self.completed_at = completed_at
        self.error_message = error_message
        self.subtasks = subtasks
        self.raw = raw
        self._api = api

    @classmethod
    def from_api(cls, data: dict, *, api: Any = None) -> "DownloadTask":
        return cls(
            id=str(data["id"]), repo_id=data["repo_id"],
            revision=data["revision"], status=data["status"],
            priority=data["priority"], created_at=str(data["created_at"]),
            completed_at=data.get("completed_at"),
            error_message=data.get("error_message"),
            subtasks=list(data.get("subtasks") or []),
            raw=data, api=api)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    def files_done(self) -> tuple[int, int]:
        total = len(self.subtasks)
        done = sum(1 for s in self.subtasks
                   if s.get("status") == "succeeded")
        return done, total

    def refresh(self) -> "DownloadTask":
        if self._api is None:
            raise RuntimeError("detached DownloadTask has no client")
        return self._api.get(self.id)

    def wait(self, timeout: float | None = None,
             on_progress=None, poll_interval: float = 5.0) -> "DownloadTask":
        import time

        from dlw.sdk.errors import Timeout
        start = time.monotonic()
        cur: DownloadTask = self
        while cur.status not in TERMINAL:
            if timeout is not None and time.monotonic() - start > timeout:
                raise Timeout(f"task {self.id} not terminal after {timeout}s")
            time.sleep(poll_interval)
            cur = self._api.get(self.id)
            if on_progress is not None:
                on_progress(cur)
        return cur
