"""v2.1 Sprint 6 — /metrics endpoint smoke test.

We don't care about specific metric values here — that's tested where
each metric is incremented (e.g. test_replication_worker.py). This file
verifies the endpoint exists, returns the right content-type, and
exposes our replication metric names so a ServiceMonitor scrape against
a fresh controller has something to read."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from tests.conftest import make_app_with_state


@pytest.fixture
async def client(ephemeral_ca):
    app = make_app_with_state(ephemeral_ca, enrollment_token="e")
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test", timeout=5.0) as c:
        yield c


async def test_metrics_endpoint_returns_prometheus_text(client: AsyncClient):
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    ctype = resp.headers.get("content-type", "")
    # prometheus_client returns text/plain; version=0.0.4; charset=utf-8
    assert "text/plain" in ctype
    body = resp.text
    # Each metric registers its HELP line on first scrape regardless of
    # whether it's been incremented yet.
    assert "dlw_replication_bytes_total" in body
    assert "dlw_replication_jobs_total" in body
    assert "dlw_replication_job_duration_seconds" in body
    # Leader-election role gauge — the overview dashboard graphs
    # `max(dlw_controller_role) by (role)`, so the series must exist.
    assert "dlw_controller_role" in body
    # Core task-lifecycle + executor-health metrics (back the overview panels).
    for name in ("dlw_tasks_active_count", "dlw_tasks_completed_total",
                 "dlw_task_duration_seconds", "dlw_executors_count",
                 "dlw_executor_status", "dlw_subtask_retries_total"):
        assert name in body, f"{name} missing from /metrics"


def test_set_controller_role_one_hots_the_gauge():
    """set_controller_role(r) sets r's gauge to 1 and every other role to 0,
    so `max(dlw_controller_role) by (role)` shows exactly one active role
    per instance."""
    from dlw.observability.metrics import CONTROLLER_ROLE, set_controller_role

    def val(role: str) -> float:
        return CONTROLLER_ROLE.labels(role=role)._value.get()

    set_controller_role("active")
    assert val("active") == 1.0
    assert val("standby") == 0.0
    assert val("recovering") == 0.0

    set_controller_role("standby")
    assert val("standby") == 1.0
    assert val("active") == 0.0
    assert val("recovering") == 0.0


def test_record_task_terminal_counts_and_times():
    from datetime import UTC, datetime, timedelta

    from dlw.observability.metrics import (
        TASK_DURATION_SECONDS,
        TASKS_COMPLETED_TOTAL,
        _reset_for_tests,
        record_task_terminal,
    )
    _reset_for_tests()
    start = datetime(2026, 1, 1, tzinfo=UTC)
    record_task_terminal("succeeded", start, start + timedelta(seconds=42))
    record_task_terminal("failed", start, start + timedelta(seconds=10))
    assert TASKS_COMPLETED_TOTAL.labels(status="succeeded")._value.get() == 1.0
    assert TASKS_COMPLETED_TOTAL.labels(status="failed")._value.get() == 1.0
    # two observations recorded in the histogram
    assert TASK_DURATION_SECONDS._sum.get() == 52.0
    # missing timestamps → counter still increments, no duration observed
    record_task_terminal("cancelled", None, None)
    assert TASKS_COMPLETED_TOTAL.labels(status="cancelled")._value.get() == 1.0
    assert TASK_DURATION_SECONDS._sum.get() == 52.0


def test_set_executor_status_one_hots_per_executor():
    from dlw.observability.metrics import (
        EXECUTOR_STATUS,
        _reset_for_tests,
        set_executor_status,
    )
    _reset_for_tests()

    def val(eid, status):
        return EXECUTOR_STATUS.labels(executor_id=eid, status=status)._value.get()

    set_executor_status("worker-1", "healthy")
    assert val("worker-1", "healthy") == 1.0
    assert val("worker-1", "faulty") == 0.0
    set_executor_status("worker-1", "faulty")
    assert val("worker-1", "faulty") == 1.0
    assert val("worker-1", "healthy") == 0.0
    # independent executors keep independent series
    set_executor_status("worker-2", "joining")
    assert val("worker-2", "joining") == 1.0
    assert val("worker-1", "faulty") == 1.0


def test_refresh_fleet_gauges_clears_stale_tenants():
    from dlw.observability.metrics import (
        EXECUTORS_COUNT,
        TASKS_ACTIVE,
        _reset_for_tests,
        refresh_fleet_gauges,
    )
    _reset_for_tests()
    refresh_fleet_gauges({1: 5, 2: 3}, online_executors=4)
    assert TASKS_ACTIVE.labels(tenant_id="1")._value.get() == 5.0
    assert EXECUTORS_COUNT._value.get() == 4.0
    # tenant 2 drops to zero active → its series must disappear, not freeze at 3
    refresh_fleet_gauges({1: 2}, online_executors=4)
    assert TASKS_ACTIVE.labels(tenant_id="1")._value.get() == 2.0
    # children are keyed by a tuple of label values, e.g. ("1",)
    labeled = {k[0] for k in TASKS_ACTIVE._metrics}
    assert "2" not in labeled


async def test_metrics_endpoint_is_unauthenticated(client: AsyncClient):
    """ServiceMonitor scrapes without auth; this guards against an
    accidental auth-middleware wiring that would 401 every Prometheus
    poll."""
    resp = await client.get("/metrics")  # no headers
    assert resp.status_code == 200
