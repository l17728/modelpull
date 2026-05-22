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
        mev = re.fullmatch(r"/api/v1/tasks/([^/]+)/events", path)
        # --- read-only routes ---
        if request.method == "GET" and path == "/api/v1/auth/me":
            return httpx.Response(200, json={
                "user_id": 1, "tenant_id": 1, "role": "tenant_admin",
                "project_ids": [1], "is_service": False})
        if request.method == "GET" and path == "/api/v1/quota/current":
            return httpx.Response(200, json={
                "tenant_id": 1, "bytes_used_month": 0,
                "bytes_quota_month": 1000, "storage_gb_used": 0,
                "storage_gb_quota": 1024, "concurrent_tasks": 0,
                "concurrent_quota": 10})
        if request.method == "GET" and path == "/api/v1/executors":
            items = [{"id": "ex-1", "status": "healthy", "health_score": 100,
                      "epoch": 1, "host_id": "h1", "tenant_id": 1,
                      "last_heartbeat_at": None, "nic_speed_gbps": 10,
                      "disk_free_gb": 900, "disk_total_gb": 1000,
                      "created_at": "2026-01-01T00:00:00"}]
            status_filter = request.url.params.get("status")
            if status_filter:
                items = [i for i in items if i["status"] == status_filter]
            return httpx.Response(200, json={"items": items})
        if mev and request.method == "GET":
            return httpx.Response(200, json={
                "items": [{"ts": "2026-01-01T00:00:00", "type": "task.created",
                           "message": "created", "details": {}}],
                "next_cursor": None})
        if request.method == "GET" and path == "/api/v1/audit/log":
            return httpx.Response(200, json={
                "items": [{"id": 1, "occurred_at": "2026-01-01T00:00:00",
                           "tenant_id": 1, "actor_user_id": 1,
                           "actor_ip": "127.0.0.1", "action": "task.created",
                           "resource_type": "task", "resource_id": "t1",
                           "outcome": "success", "payload": {},
                           "trace_id": "tr1", "prev_hash": None,
                           "self_hash": "h1"}],
                "next_cursor": None})
        # --- task write routes ---
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
