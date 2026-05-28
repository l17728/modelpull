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

from prometheus_client import Counter, Gauge, Histogram

# ---------------------------------------------------------------------------
# Controller leader-election role (Phase 2 W3c — active/standby)

CONTROLLER_ROLE = Gauge(
    "dlw_controller_role",
    "Leader-election role of this controller instance. One-hot: the gauge "
    "for the current role is 1, the others 0. role ∈ "
    "{standby, recovering, active}. The overview dashboard graphs "
    "`max(dlw_controller_role) by (role)` to show, across instances, which "
    "role is occupied.",
    labelnames=("role",),
)

_CONTROLLER_ROLES = ("standby", "recovering", "active")


def set_controller_role(role: str) -> None:
    """One-hot the dlw_controller_role gauge to `role` (set it to 1, all
    other roles to 0). Called from the leader loop's state-transition hook
    so the metric tracks active/standby/recovering in real time."""
    for r in _CONTROLLER_ROLES:
        CONTROLLER_ROLE.labels(role=r).set(1.0 if r == role else 0.0)


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

# ---------------------------------------------------------------------------
# Core task lifecycle + executor health (the operator's "is it working /
# are my workers alive" signals — back the overview dashboard's top panels).

TASKS_ACTIVE = Gauge(
    "dlw_tasks_active_count",
    "Download tasks in a non-terminal state (pending/scheduling/downloading/"
    "cancelling), partitioned by tenant. Refreshed by the active controller's "
    "sweep loop. The overview dashboard's tenant dropdown reads this label.",
    labelnames=("tenant_id",),
)

TASKS_COMPLETED_TOTAL = Counter(
    "dlw_tasks_completed_total",
    "Download tasks that reached a terminal state, by status "
    "(succeeded/failed/cancelled). Incremented at the terminal transition.",
    labelnames=("status",),
)

TASK_DURATION_SECONDS = Histogram(
    "dlw_task_duration_seconds",
    "Wall time from task creation to terminal state. Buckets span seconds "
    "(tiny repos) to hours (TB-scale multi-file models).",
    buckets=(1, 5, 30, 60, 300, 1800, 3600, 7200, 21600, 43200),
)

EXECUTORS_COUNT = Gauge(
    "dlw_executors_count",
    "Online executors (registered and not faulty). Refreshed by the active "
    "controller's sweep loop.",
)

EXECUTOR_STATUS = Gauge(
    "dlw_executor_status",
    "Per-executor health, one-hot: the gauge for the executor's current "
    "status is 1, the others 0. status ∈ "
    "{joining, healthy, degraded, suspect, faulty}.",
    labelnames=("executor_id", "status"),
)

# Keep in sync with state_machine.VALID_STATUS (duplicated here to avoid an
# import cycle: state_machine emits this metric, so it can't import nothing
# heavier from us).
_EXECUTOR_STATUSES = ("joining", "healthy", "degraded", "suspect", "faulty")

SUBTASK_RETRIES_TOTAL = Counter(
    "dlw_subtask_retries_total",
    "Cumulative file-subtask retries issued (reclaim on executor timeout + "
    "startup recovery re-queue). A rising rate signals flaky workers/sources.",
)


def record_task_terminal(status: str, created_at, completed_at) -> None:
    """Emit the completed-counter + duration-histogram for one task reaching
    a terminal state. Call from the single terminal-transition seam(s)."""
    TASKS_COMPLETED_TOTAL.labels(status=status).inc()
    if created_at is not None and completed_at is not None:
        TASK_DURATION_SECONDS.observe((completed_at - created_at).total_seconds())


def set_executor_status(executor_id: str, status: str) -> None:
    """One-hot the per-executor status gauge (current status 1, others 0)."""
    for s in _EXECUTOR_STATUSES:
        EXECUTOR_STATUS.labels(executor_id=str(executor_id), status=s).set(
            1.0 if s == status else 0.0)


def refresh_fleet_gauges(active_by_tenant: dict, online_executors: int) -> None:
    """Reset + repopulate the periodically-sampled gauges. `.clear()` drops
    stale tenant children so a tenant that fell to zero active tasks doesn't
    leave a frozen non-zero series."""
    TASKS_ACTIVE.clear()
    for tenant_id, n in active_by_tenant.items():
        TASKS_ACTIVE.labels(tenant_id=str(tenant_id)).set(n)
    EXECUTORS_COUNT.set(online_executors)


def _reset_for_tests() -> None:
    """Reset all counters in this module. Tests that assert on metric
    values use this in an autouse fixture so a previous test's bumps
    don't poison the next assertion.

    Labeled metrics (Counter/Histogram with labelnames=) keep their
    children in `_metrics`; unlabeled metrics use `_sum` / `_value` /
    `_buckets` directly. Handle both."""
    for metric in (CONTROLLER_ROLE,
                   REPLICATION_BYTES_TOTAL,
                   REPLICATION_JOBS_TOTAL,
                   REPLICATION_JOB_DURATION_SECONDS,
                   OPTIMIZER_SOLVE_DURATION_SECONDS,
                   REPLAN_CHUNK_MOVES_TOTAL,
                   TASKS_ACTIVE,
                   TASKS_COMPLETED_TOTAL,
                   TASK_DURATION_SECONDS,
                   EXECUTORS_COUNT,
                   EXECUTOR_STATUS,
                   SUBTASK_RETRIES_TOTAL):
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
