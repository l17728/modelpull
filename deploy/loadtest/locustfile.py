"""v2.1 Sprint 15 — Locust load-test profile.

Run with:
  locust -f deploy/loadtest/locustfile.py \
         --headless -u 100 -r 10 -t 7d \
         --csv staging-baseline

Required env:
  DLW_BASE_URL   — full base URL of the controller (no trailing slash)
  DLW_JWT        — tenant_admin JWT (read + create)
  DLW_ADMIN_JWT  — system_admin JWT (admin-only endpoints)

The locustfile is intentionally additive: each user type focuses on
one v2.x surface so a perf regression points back to the responsible
sprint without correlating multiple users."""
from __future__ import annotations

import os
import random
import time

from locust import HttpUser, between, task


def _auth_headers(token_env: str = "DLW_JWT") -> dict[str, str]:
    return {"Authorization": f"Bearer {os.environ[token_env]}"}


class BrowsingUser(HttpUser):
    """60% of traffic — passive read load. Exercises tenant scoping
    everywhere; sees the same isolation as the dashboard UI."""
    weight = 60
    wait_time = between(2, 6)

    @task(5)
    def quota(self) -> None:
        self.client.get("/api/v1/quota/current",
                         headers=_auth_headers(),
                         name="GET /quota/current")

    @task(3)
    def tasks_running(self) -> None:
        self.client.get("/api/v1/tasks?status=running&limit=50",
                         headers=_auth_headers(),
                         name="GET /tasks?status=running")

    @task(2)
    def audit_tail(self) -> None:
        self.client.get("/api/v1/audit?limit=25",
                         headers=_auth_headers(),
                         name="GET /audit")

    @task(1)
    def storages(self) -> None:
        self.client.get("/api/v1/storages",
                         headers=_auth_headers(),
                         name="GET /storages")


class SubmittingUser(HttpUser):
    """20% of traffic — task creators. Steady ~1 task/30s/user covers
    the create + assign + run pipeline. Repo IDs rotate to keep dedupe
    and increment paths both warm."""
    weight = 20
    wait_time = between(20, 40)

    REPOS = (
        "meta-llama/Llama-3-8B",
        "Qwen/Qwen2-7B-Instruct",
        "deepseek-ai/DeepSeek-R1",
        "mistralai/Mistral-7B-v0.1",
    )

    @task
    def submit(self) -> None:
        repo = random.choice(self.REPOS)
        self.client.post(
            "/api/v1/tasks",
            headers=_auth_headers(),
            json={"repo_id": repo, "revision": "main"},
            name="POST /tasks")


class AdminUser(HttpUser):
    """15% of traffic — operator surfaces. Validates the v2.1 admin
    REST endpoints under load (reverse-WSS sessions list, GC status,
    replication list)."""
    weight = 15
    wait_time = between(5, 15)

    @task(2)
    def sessions(self) -> None:
        self.client.get("/api/v1/admin/reverse-ws/sessions",
                         headers=_auth_headers("DLW_ADMIN_JWT"),
                         name="GET /admin/reverse-ws/sessions")

    @task(2)
    def gc_status(self) -> None:
        self.client.get("/api/v1/admin/gc/status",
                         headers=_auth_headers("DLW_ADMIN_JWT"),
                         name="GET /admin/gc/status")

    @task(1)
    def replication(self) -> None:
        self.client.get("/api/v1/replication?status=pending",
                         headers=_auth_headers(),
                         name="GET /replication")


class AIUser(HttpUser):
    """5% of traffic — AI assistant. Read-only tool flows so the
    suggester path is exercised without polluting download volume."""
    weight = 5
    wait_time = between(15, 60)

    @task
    def list_tasks_via_chat(self) -> None:
        self.client.post(
            "/api/v1/ai/chat",
            headers=_auth_headers(),
            json={"prompt": "show my running tasks"},
            name="POST /ai/chat (read-only)")


# --- Sanity check: fail fast if env is incomplete --------------------------

def _preflight() -> None:
    for var in ("DLW_BASE_URL", "DLW_JWT", "DLW_ADMIN_JWT"):
        if not os.environ.get(var):
            raise SystemExit(
                f"locustfile preflight: ${var} is unset; refusing to "
                f"start a 7-day run without auth.")


_preflight()
