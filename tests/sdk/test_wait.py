"""DownloadTask.wait: polls refresh() until terminal / times out (SP4)."""
from __future__ import annotations

import pytest

from dlw.sdk.errors import Timeout
from dlw.sdk.models import DownloadTask


class _StubAPI:
    """get() yields the queued statuses in order, repeating the last."""
    def __init__(self, statuses):
        self._q = list(statuses)

    def get(self, _id):
        st = self._q.pop(0) if len(self._q) > 1 else self._q[0]
        return DownloadTask.from_api({
            "id": "t", "repo_id": "o/r", "revision": "r", "status": st,
            "priority": 1, "created_at": "2026-05-19T00:00:00Z",
            "completed_at": None, "error_message": None, "subtasks": []},
            api=self)


def _task(status, api):
    return DownloadTask.from_api({
        "id": "t", "repo_id": "o/r", "revision": "r", "status": status,
        "priority": 1, "created_at": "2026-05-19T00:00:00Z",
        "completed_at": None, "error_message": None, "subtasks": []},
        api=api)


def test_returns_immediately_when_already_terminal():
    api = _StubAPI(["succeeded"])
    t = _task("succeeded", api)
    assert t.wait(poll_interval=0).status == "succeeded"


def test_polls_until_terminal_and_calls_on_progress():
    api = _StubAPI(["downloading", "downloading", "succeeded"])
    seen: list[str] = []
    t = _task("downloading", api)
    out = t.wait(poll_interval=0, on_progress=lambda x: seen.append(x.status))
    assert out.status == "succeeded"
    assert seen and seen[-1] == "succeeded"


def test_timeout_raises():
    api = _StubAPI(["downloading"])
    t = _task("downloading", api)
    with pytest.raises(Timeout):
        t.wait(timeout=0.01, poll_interval=0.005)
