"""Tests for ControllerClient using httpx MockTransport — no real network."""
from __future__ import annotations

import json
import uuid

import httpx
import pytest

from dlw.executor.client import ControllerClient


def _mock_handler(request: httpx.Request) -> httpx.Response:
    """Routes requests to canned responses based on URL path."""
    path = request.url.path
    body = json.loads(request.content) if request.content else {}

    if path == "/api/v1/executors/join" and request.method == "POST":
        return httpx.Response(201, json={
            "id": body["id"], "status": "joining", "health_score": 100,
        })
    if path.endswith("/heartbeat") and request.method == "POST":
        return httpx.Response(200, json={
            "id": "x", "status": "healthy", "health_score": body.get("health_score", 100),
        })
    if path.endswith("/poll") and request.method == "POST":
        return httpx.Response(200, json={
            "assigned": True,
            "subtask": {
                "id": str(uuid.uuid4()),
                "task_id": str(uuid.uuid4()),
                "filename": "model.safetensors",
                "file_size": 1024,
                "expected_sha256": None,
                "status": "assigned",
            },
            "assignment_token": str(uuid.uuid4()),
        })
    if "/subtasks/" in path and path.endswith("/report"):
        return httpx.Response(200, json={
            "subtask_status": "succeeded", "task_status": "pending",
        })
    return httpx.Response(404)


@pytest.fixture
def transport() -> httpx.MockTransport:
    return httpx.MockTransport(_mock_handler)


@pytest.mark.slow
async def test_join_sends_correct_body(transport) -> None:
    async with ControllerClient(
        base_url="http://test", bearer_token="t", _transport=transport
    ) as c:
        r = await c.join(executor_id="ex-1", host_id="h", capabilities={"nic_speed_gbps": 10})
    assert r["status"] == "joining"


@pytest.mark.slow
async def test_heartbeat_returns_state(transport) -> None:
    async with ControllerClient(
        base_url="http://test", bearer_token="t", _transport=transport
    ) as c:
        r = await c.heartbeat(executor_id="ex-1", health_score=88, parts_dir_bytes=0)
    assert r["status"] == "healthy"
    assert r["health_score"] == 88


@pytest.mark.slow
async def test_poll_returns_assignment(transport) -> None:
    async with ControllerClient(
        base_url="http://test", bearer_token="t", _transport=transport
    ) as c:
        r = await c.poll(executor_id="ex-1")
    assert r["assigned"] is True
    assert "subtask" in r
    assert "assignment_token" in r


@pytest.mark.slow
async def test_report_propagates_token(transport) -> None:
    async with ControllerClient(
        base_url="http://test", bearer_token="t", _transport=transport
    ) as c:
        r = await c.report(
            subtask_id=uuid.uuid4(),
            status="succeeded",
            assignment_token=uuid.uuid4(),
            actual_sha256="a" * 64,
            bytes_downloaded=1024,
        )
    assert r["subtask_status"] == "succeeded"


@pytest.mark.slow
async def test_unauthenticated_returns_401(transport) -> None:
    """ControllerClient should propagate 401 as an exception (caller decides retry)."""
    def unauth(_):
        return httpx.Response(401, json={"detail": "missing bearer token"})
    t = httpx.MockTransport(unauth)
    async with ControllerClient(base_url="http://test", bearer_token="bad", _transport=t) as c:
        with pytest.raises(httpx.HTTPStatusError):
            await c.heartbeat(executor_id="ex-1", health_score=100, parts_dir_bytes=0)
