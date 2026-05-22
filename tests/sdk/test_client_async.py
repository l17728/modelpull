"""async AsyncClient over the REAL ASGI app + DB (SP4).

Uses the async `aclient` fixture from _fixtures.py (same loop as the
session-scoped engine — mirrors tests/api/test_tasks.py exactly)."""
from __future__ import annotations

import pytest

from dlw.sdk import errors as e
from tests.sdk._fixtures import *  # noqa: F401,F403  (fixtures + __all__)

pytestmark = pytest.mark.slow


async def test_async_submit_get_list_cancel(aclient):
    t = await aclient.tasks.submit(repo_id="o/r", revision="0" * 40,
                                   storage_id=1)
    assert t.status == "pending"
    got = await aclient.tasks.get(t.id)
    assert got.id == t.id
    assert len(got.subtasks) == 2          # TaskDetail, patched HF -> 2
    again = await got.refresh()
    assert again.id == t.id
    lst = await aclient.tasks.list(status="pending")
    assert any(x.id == t.id for x in lst)
    await aclient.tasks.cancel(t.id)
    cur = await aclient.tasks.get(t.id)
    assert cur.status == "cancelling"      # R3: never "cancelled" in tests


async def test_async_delete_non_terminal_conflict(aclient):
    t = await aclient.tasks.submit(repo_id="o/x", revision="4" * 40,
                                   storage_id=1)
    with pytest.raises(e.Conflict):
        await aclient.tasks.delete(t.id)


async def test_me_async(aclient):
    me = await aclient.me()
    assert me["tenant_id"] == 1


async def test_quota_async(aclient):
    q = await aclient.quota.current()
    assert "bytes_quota_month" in q and "storage_gb_used" in q


async def test_executors_async(aclient):
    assert "items" in await aclient.executors.list()


async def test_audit_async(aclient):
    assert "items" in await aclient.audit.search(limit=10)


async def test_tasks_events_async(aclient):
    t = await aclient.tasks.submit(repo_id="o/ev", revision="e" * 40,
                                   storage_id=1)
    result = await aclient.tasks.events(t.id)
    assert "items" in result


async def test_tasks_events_stream_async(aclient):
    t = await aclient.tasks.submit(repo_id="o/es", revision="f" * 40,
                                   storage_id=1)
    async with aclient.tasks.events_stream(t.id, max_ticks=1) as r:
        lines = [ln async for ln in r.aiter_lines() if ln]
    assert any(ln.startswith("data:") or ln.startswith(":open") for ln in lines)


async def test_task_stream_async(aclient):
    t = await aclient.tasks.submit(repo_id="o/r", revision="b" * 40,
                                   storage_id=1)
    lines = []
    async with aclient.tasks.task_stream(t.id, max_ticks=1) as r:
        async for line in r.aiter_lines():
            lines.append(line)
    assert any(l.startswith("data: ") or l.startswith(":open") for l in lines)


async def test_async_wait_polls_until_terminal():
    from dlw.sdk.aclient import AsyncDownloadTask

    class _AStub:
        def __init__(self, sts):
            self._q = list(sts)

        async def get(self, _id):
            st = self._q.pop(0) if len(self._q) > 1 else self._q[0]
            return AsyncDownloadTask.from_api({
                "id": "t", "repo_id": "o", "revision": "r", "status": st,
                "priority": 1, "created_at": "2026-05-19T00:00:00Z",
                "completed_at": None, "error_message": None,
                "subtasks": []}, api=self)

    api = _AStub(["downloading", "succeeded"])
    t = AsyncDownloadTask.from_api({
        "id": "t", "repo_id": "o", "revision": "r", "status": "downloading",
        "priority": 1, "created_at": "2026-05-19T00:00:00Z",
        "completed_at": None, "error_message": None, "subtasks": []},
        api=api)
    out = await t.wait(poll_interval=0)
    assert out.status == "succeeded"
