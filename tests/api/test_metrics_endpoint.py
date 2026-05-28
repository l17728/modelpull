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


async def test_metrics_endpoint_is_unauthenticated(client: AsyncClient):
    """ServiceMonitor scrapes without auth; this guards against an
    accidental auth-middleware wiring that would 401 every Prometheus
    poll."""
    resp = await client.get("/metrics")  # no headers
    assert resp.status_code == 200
