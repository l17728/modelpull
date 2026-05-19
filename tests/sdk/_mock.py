"""Stateful httpx.MockTransport mirroring the real /api/v1/tasks surface.

Used only by the sync Client + CLI tests (httpx 0.27.2 ASGITransport is
async-only). Realistic FastAPI-shaped bodies/status; the async e2e
(test_client_async.py) validates these shapes against the real app."""
from __future__ import annotations

import json
import re
import uuid

import httpx

_VALID = "Bearer good"
TERMINAL = {"succeeded", "failed", "cancelled"}


def make_mock_transport() -> httpx.MockTransport:
    store: dict[str, dict] = {}

    def _task(repo, rev, status="pending"):
        return {"id": str(uuid.uuid4()), "repo_id": repo, "revision": rev,
                "status": status, "priority": 1,
                "created_at": "2026-05-19T00:00:00Z",
                "completed_at": None, "error_message": None}

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        if auth != _VALID:
            return httpx.Response(401, json={"detail": "unauthenticated"})
        path = request.url.path
        m = re.fullmatch(r"/api/v1/tasks/([^/]+)", path)
        mc = re.fullmatch(r"/api/v1/tasks/([^/]+)/cancel", path)
        if request.method == "POST" and path == "/api/v1/tasks":
            body = json.loads(request.content or b"{}")
            t = _task(body["repo_id"], body["revision"])
            store[t["id"]] = {**t, "subtasks": []}
            return httpx.Response(201, json=t)          # TaskRead (no subtasks)
        if request.method == "GET" and path == "/api/v1/tasks":
            return httpx.Response(200, json={
                "items": [{k: v for k, v in t.items() if k != "subtasks"}
                          for t in store.values()],
                "total": len(store)})
        if mc and request.method == "POST":
            t = store.get(mc.group(1))
            if t is None:
                return httpx.Response(404, json={"detail": "task not found"})
            t["status"] = "cancelling"
            return httpx.Response(202, json={
                k: v for k, v in t.items() if k != "subtasks"})
        if m and request.method == "GET":
            t = store.get(m.group(1))
            if t is None:
                return httpx.Response(404, json={"detail": "task not found"})
            return httpx.Response(200, json={**t, "subtasks": [
                {"status": "pending"}, {"status": "pending"}]})  # TaskDetail
        if m and request.method == "DELETE":
            t = store.get(m.group(1))
            if t is None:
                return httpx.Response(404, json={"detail": "task not found"})
            if t["status"] not in TERMINAL:
                return httpx.Response(409, json={"detail": {
                    "code": "TASK_NOT_TERMINAL", "status": t["status"]}})
            del store[m.group(1)]
            return httpx.Response(204)
        return httpx.Response(404, json={"detail": "not found"})

    return httpx.MockTransport(handler)


GOOD_TOKEN = "good"   # the SDK sends "Bearer good"; _mock accepts only that
