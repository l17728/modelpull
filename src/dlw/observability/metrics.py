"""v2.1 Sprint 6 — Prometheus metrics foundation.

Centralizes every Counter / Histogram / Gauge so:
  - exporters get a single registry (default global)
  - tests can reset all metrics between cases via `_reset_for_tests()`
  - dashboards have one place to consult for metric names + labels

This file is intentionally narrow — it only defines metric handles.
The actual increment calls live in the service modules they measure
(e.g. replication_worker.py). That keeps the metric semantics next to
the code that owns them while keeping the names discoverable here."""
from __future__ import annotations

from prometheus_client import Counter, Histogram

# ---------------------------------------------------------------------------
# Cross-region replication (Sprint 5 / Sprint 6)

REPLICATION_BYTES_TOTAL = Counter(
    "dlw_replication_bytes_total",
    "Total bytes transferred by the replication worker, partitioned by "
    "tenant + target_storage_id + terminal status. The status label is "
    "set when the job reaches a terminal state.",
    labelnames=("tenant_id", "target_storage_id", "status"),
)

REPLICATION_JOBS_TOTAL = Counter(
    "dlw_replication_jobs_total",
    "Replication jobs that reached a terminal state, partitioned by "
    "tenant + status. status ∈ {succeeded, failed, cancelled, skipped_existing}.",
    labelnames=("tenant_id", "status"),
)

REPLICATION_JOB_DURATION_SECONDS = Histogram(
    "dlw_replication_job_duration_seconds",
    "Wall time from claim → terminal for one replication job. Includes "
    "retry backoffs. Buckets sized for typical 100MB-10GB transfers.",
    labelnames=("status",),
    buckets=(0.1, 0.5, 1, 5, 10, 30, 60, 300, 1800, 3600),
)


def _reset_for_tests() -> None:
    """Reset all counters in this module. Tests that assert on metric
    values use this in an autouse fixture so a previous test's bumps
    don't poison the next assertion."""
    for metric in (REPLICATION_BYTES_TOTAL,
                   REPLICATION_JOBS_TOTAL,
                   REPLICATION_JOB_DURATION_SECONDS):
        metric._metrics.clear()  # type: ignore[attr-defined]
