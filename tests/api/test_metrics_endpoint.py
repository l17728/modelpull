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


async def test_metrics_endpoint_is_unauthenticated(client: AsyncClient):
    """ServiceMonitor scrapes without auth; this guards against an
    accidental auth-middleware wiring that would 401 every Prometheus
    poll."""
    resp = await client.get("/metrics")  # no headers
    assert resp.status_code == 200
