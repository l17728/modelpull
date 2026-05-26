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

# ---------------------------------------------------------------------------
# Adaptive optimizer (Sprint 9)

OPTIMIZER_SOLVE_DURATION_SECONDS = Histogram(
    "dlw_optimizer_solve_duration_seconds",
    "Wall time for one optimizer.solve() call. Sub-second buckets so we "
    "can see if the LPT heuristic stops fitting the workload.",
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0),
)

REPLAN_CHUNK_MOVES_TOTAL = Counter(
    "dlw_replan_chunk_moves_total",
    "Cumulative chunks the replan loop moved to a different source. "
    "'shadow' counts moves the loop COMPUTED but did NOT apply (feature "
    "flag off); 'apply' counts moves actually persisted.",
    labelnames=("mode",),
)


def _reset_for_tests() -> None:
    """Reset all counters in this module. Tests that assert on metric
    values use this in an autouse fixture so a previous test's bumps
    don't poison the next assertion.

    Labeled metrics (Counter/Histogram with labelnames=) keep their
    children in `_metrics`; unlabeled metrics use `_sum` / `_value` /
    `_buckets` directly. Handle both."""
    for metric in (REPLICATION_BYTES_TOTAL,
                   REPLICATION_JOBS_TOTAL,
                   REPLICATION_JOB_DURATION_SECONDS,
                   OPTIMIZER_SOLVE_DURATION_SECONDS,
                   REPLAN_CHUNK_MOVES_TOTAL):
        children = getattr(metric, "_metrics", None)
        if children is not None:
            children.clear()
            continue
        # Unlabeled — zero its internal state. Counter has _value,
        # Histogram has _sum + _buckets.
        if hasattr(metric, "_value"):
            metric._value.set(0)  # type: ignore[attr-defined]
        if hasattr(metric, "_sum"):
            metric._sum.set(0)  # type: ignore[attr-defined]
        if hasattr(metric, "_buckets"):
            for b in metric._buckets:  # type: ignore[attr-defined]
                b.set(0)
