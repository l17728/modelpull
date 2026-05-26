"""v2.1 Sprint 6 — Prometheus scrape endpoint.

Exposes the default registry at /metrics so the cluster's
ServiceMonitor (deploy/helm/templates/servicemonitor.yaml) can scrape
it. Public on purpose — Prometheus operators commonly scrape without
auth and the metrics are operational counters (no PII).

If a future deployment needs auth-gated metrics, swap this for an
mTLS-gated route or move to a sidecar exporter; the metric definitions
themselves (observability/metrics.py) are unchanged."""
from __future__ import annotations

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

# Import the metrics module so its Counter/Histogram definitions register
# themselves with the default REGISTRY on app import (before the first
# scrape). Otherwise the very first /metrics response would be empty.
import dlw.observability.metrics  # noqa: F401

router = APIRouter(tags=["metrics"])


@router.get("/metrics")
def get_metrics() -> Response:
    return Response(content=generate_latest(),
                    media_type=CONTENT_TYPE_LATEST)
