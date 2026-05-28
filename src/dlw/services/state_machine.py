"""Executor status state machine (Phase 2 W2a, spec §6).

ALL Python attribute writes to Executor.status MUST go through
transition_executor(). CI lint (tools/lint_no_direct_status_write.py)
enforces this. The atomic INSERT-or-bump in services.executor_service
.join_executor uses pg_insert.values(status=...) — a SQL-builder kwarg,
not a Python attribute assignment — and is correctly outside the lint's
pattern.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.executor import Executor
from dlw.db.models.executor_status_history import ExecutorStatusHistory

logger = logging.getLogger(__name__)

Event = Literal[
    "heartbeat_ok", "heartbeat_timeout",
    "task_success", "task_failure", "admin",
]


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw is not None else default


HB_TIMEOUT_TO_SUSPECT  = _env_int("DLW_HB_TIMEOUT_TO_SUSPECT", 3)
HB_TIMEOUT_TO_FAULTY   = _env_int("DLW_HB_TIMEOUT_TO_FAULTY", 6)
TASK_FAIL_TO_DEGRADED  = _env_int("DLW_TASK_FAIL_TO_DEGRADED", 3)
DEGRADED_STREAK_FAULTY = _env_int("DLW_DEGRADED_STREAK_FAULTY", 10)
DEGRADED_RECOVER_OK    = _env_int("DLW_DEGRADED_RECOVER_OK", 5)

VALID_STATUS = ("joining", "healthy", "degraded", "suspect", "faulty")


@dataclass(frozen=True)
class Transition:
    from_status: str
    to_status: str
    reason: str
    event: Event


async def transition_executor(
    session: AsyncSession,
    ex: Executor,
    *,
    event: Event,
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Transition | None:
    """Mutate executor counters + status per spec §6.

    Returns Transition if status changed (and one history row is appended),
    None if only counters moved. Caller commits. Caller is responsible for
    side effects of a transition (e.g. reclaim_subtasks on → suspect/faulty).
    """
    from_status = ex.status
    to_status, transition_reason = from_status, reason

    if event == "heartbeat_ok":
        ex.consecutive_heartbeat_failures = 0
        ex.last_heartbeat_at = datetime.now(UTC)
        if from_status == "joining":
            to_status, transition_reason = "healthy", "first_hb"
        elif from_status == "suspect":
            to_status, transition_reason = "degraded", "hb_recovered"

    elif event == "heartbeat_timeout":
        if from_status == "joining":
            logger.warning(
                "transition_executor: heartbeat_timeout on joining executor %s; no-op",
                ex.id,
            )
            return None
        ex.consecutive_heartbeat_failures += 1
        n = ex.consecutive_heartbeat_failures
        if from_status == "healthy" and n >= HB_TIMEOUT_TO_SUSPECT:
            to_status, transition_reason = "suspect", f"hb_timeout_{HB_TIMEOUT_TO_SUSPECT}"
        elif from_status == "degraded" and n >= HB_TIMEOUT_TO_SUSPECT:
            to_status = "suspect"
            transition_reason = f"hb_timeout_{HB_TIMEOUT_TO_SUSPECT}_from_degraded"
        elif from_status == "suspect" and n >= HB_TIMEOUT_TO_FAULTY:
            to_status, transition_reason = "faulty", f"hb_timeout_{HB_TIMEOUT_TO_FAULTY}"

    elif event == "task_success":
        if from_status in ("suspect", "faulty", "joining"):
            logger.warning(
                "transition_executor: task_success on %s executor %s; no-op",
                from_status, ex.id,
            )
            return None
        ex.consecutive_task_failures = 0
        if from_status == "degraded":
            ex.degraded_recoveries += 1
            if ex.degraded_recoveries >= DEGRADED_RECOVER_OK:
                to_status, transition_reason = "healthy", f"recovered_{DEGRADED_RECOVER_OK}_ok"

    elif event == "task_failure":
        if from_status in ("suspect", "faulty", "joining"):
            logger.warning(
                "transition_executor: task_failure on %s executor %s; no-op",
                from_status, ex.id,
            )
            return None
        if from_status == "healthy":
            ex.consecutive_task_failures += 1
            if ex.consecutive_task_failures >= TASK_FAIL_TO_DEGRADED:
                to_status = "degraded"
                transition_reason = f"{TASK_FAIL_TO_DEGRADED}_task_failures"
        elif from_status == "degraded":
            ex.degraded_failure_streak += 1
            if ex.degraded_failure_streak >= DEGRADED_STREAK_FAULTY:
                to_status = "faulty"
                transition_reason = f"degraded_streak_{DEGRADED_STREAK_FAULTY}"

    elif event == "admin":
        # Caller provides target via metadata["to_status"]; reason is the operator note.
        if metadata and "to_status" in metadata:
            to_status = metadata["to_status"]
            transition_reason = reason or "admin"

    if to_status == from_status:
        return None

    # Reset counters when entering healthy (degraded → healthy or joining → healthy).
    if to_status == "healthy":
        ex.degraded_failure_streak = 0
        ex.degraded_recoveries = 0
        ex.consecutive_task_failures = 0

    # Reset counter when leaving degraded for faulty (anti-pump: any future
    # task_failure on faulty is a no-op, but if admin resets to degraded later
    # the streak should start fresh).
    if from_status == "degraded" and to_status == "faulty":
        ex.degraded_failure_streak = 0
        ex.degraded_recoveries = 0

    transition = Transition(
        from_status=from_status,
        to_status=to_status,
        reason=transition_reason or "",
        event=event,
    )
    session.add(ExecutorStatusHistory(
        executor_id=ex.id,
        from_status=from_status,
        to_status=to_status,
        event=event,
        reason=transition.reason,
        metadata_=dict(metadata or {}),
    ))
    ex.status = to_status
    from dlw.observability.metrics import set_executor_status
    set_executor_status(ex.id, to_status)
    logger.info(
        "executor_transition: id=%s %s->%s event=%s reason=%s",
        ex.id, from_status, to_status, event, transition.reason,
    )
    return transition
