# W3c Active/Standby Controller Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add app-level controller leader election via a PG session advisory lock so two controller instances run as active/standby with automatic failover (closes OPS-04 / Phase 2 exit "RTO ≤ 10min").

**Architecture:** A `LeaderElector` holds `pg_try_advisory_lock(<active_lock_id>)` on a dedicated NullPool engine; an async `run_leader_loop` drives a `standby → recovering → active` state machine, gating `run_recovery_routine` and the sweep loop on leadership. `GET /health/active` is the LB target (200 iff leader); a `require_not_recovering` FastAPI dep returns 503 on the three executor-loop endpoints while `recovering` (INVARIANT 33).

**Tech Stack:** SQLAlchemy 2.x async + asyncpg (existing) + `pg_try_advisory_lock` (built into PostgreSQL); FastAPI dependencies; pytest with the existing local PG fixture. **No new runtime/dev deps, no new CI jobs, zero alembic migrations.**

**Spec:** `docs/superpowers/specs/2026-05-15-phase-2-w3c-active-standby-design.md`

**Branch:** `feat/phase-2-w3c-active-standby` (already created off `main` at `2924b6e` — the W3b PR merge commit).

---

## File Structure

**New files:**
- `src/dlw/services/leader_election.py` — `LeaderElector` (lock primitive) + `run_leader_loop` (state-machine coroutine). One file, two cohesive symbols. No HTTP, no FastAPI imports — pure async logic, testable in isolation.
- `src/dlw/api/_recovery_barrier.py` — `require_not_recovering` FastAPI dependency (one tiny function).
- `tests/services/test_leader_election.py` — unit/integration tests for `LeaderElector` (~6 cases against local PG).
- `tests/services/test_leader_loop.py` — unit tests for `run_leader_loop` driven by fake callbacks (~4 cases).
- `tests/api/test_health_active.py` — tests for the new `/health/active` endpoint (~3 cases).
- `tests/api/test_recovery_barrier.py` — tests for the 503 barrier (~3 cases).
- `tests/e2e/test_failover_drill.py` — the W3c centerpiece: two real `LeaderElector` instances driving a failover scenario (~3 cases).

**Modified files:**
- `src/dlw/config.py` — `Settings` gains `active_lock_id` + `leader_poll_interval_seconds`.
- `tests/test_config.py` — extend with the two new field tests.
- `src/dlw/api/health.py` — add `GET /health/active`.
- `src/dlw/api/executors.py` — attach `Depends(require_not_recovering)` to heartbeat + poll routes.
- `src/dlw/api/subtasks.py` — attach `Depends(require_not_recovering)` to the report route.
- `tests/conftest.py` — add `make_app_with_state` helper.
- `tests/api/test_executors.py`, `test_register_endpoint.py`, `test_renew_endpoint.py`, `test_subtasks.py`, `test_hf_proxy.py` — migrate the 4-line `app.state.*` block to a single `make_app_with_state(...)` call (5 fixture sites).
- `tests/e2e/test_executor_e2e.py`, `tests/e2e/test_happy_path.py` — same migration (2 fixture sites). Total ~7 fixture sites.
- `src/dlw/main.py` — lifespan restructure: `run_recovery_routine` + `_sweep_loop_main` become leader-gated; `DLW_STRICT_RECOVERY` env knob deleted.
- `tests/executor/test_client.py` — append one test asserting tenacity retries past a 503 `CONTROLLER_RECOVERING` response.
- `api/openapi.yaml` — document `/health/active` + 503 `CONTROLLER_RECOVERING` on the three executor-loop routes.
- `docs/operator/executor-runbook.md` — add a "Controller leadership" subsection.

**Out of scope (do NOT touch):**
- `deploy/runbooks/scripts/promote-standby.sh` — it's a PG-level failover script, orthogonal to W3c app-level leadership. A one-liner clarifying comment is added in T8 if appropriate, but the script's logic stays.
- Any Helm / k8s Service / LB YAML — deploy-side wiring is a deploy task, not a controller task.

---

## Milestone 1 — Lock primitive

### Task 1: Add `active_lock_id` + `leader_poll_interval_seconds` to controller Settings

**Files:**
- Modify: `src/dlw/config.py:39-40`
- Test: `tests/test_config.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py`:

```python
def test_settings_has_active_lock_id_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DLW_ACTIVE_LOCK_ID", raising=False)
    s = Settings()
    assert s.active_lock_id == 0x444C5743_414B5631


def test_settings_active_lock_id_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DLW_ACTIVE_LOCK_ID", "12345")
    s = Settings()
    assert s.active_lock_id == 12345


def test_settings_active_lock_id_rejects_zero() -> None:
    with pytest.raises(ValidationError):
        Settings(active_lock_id=0)


def test_settings_active_lock_id_rejects_above_pg_bigint_max() -> None:
    with pytest.raises(ValidationError):
        Settings(active_lock_id=9_223_372_036_854_775_808)


def test_settings_has_leader_poll_interval_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DLW_LEADER_POLL_INTERVAL_SECONDS", raising=False)
    s = Settings()
    assert s.leader_poll_interval_seconds == 5.0


def test_settings_leader_poll_interval_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DLW_LEADER_POLL_INTERVAL_SECONDS", "10.0")
    s = Settings()
    assert s.leader_poll_interval_seconds == 10.0


def test_settings_leader_poll_interval_rejects_below_min() -> None:
    with pytest.raises(ValidationError):
        Settings(leader_poll_interval_seconds=0.1)


def test_settings_leader_poll_interval_rejects_above_max() -> None:
    with pytest.raises(ValidationError):
        Settings(leader_poll_interval_seconds=99.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v`
Expected: 6 new tests fail — `AttributeError: 'Settings' object has no attribute 'active_lock_id'` (and similar for the poll-interval field).

- [ ] **Step 3: Add the fields**

In `src/dlw/config.py`, after the W3b block (`hf_proxy_timeout_seconds: int = Field(default=300, ge=10, le=3600)`) and before the `@property` `db_url`, add:

```python
    # Phase 2 W3c — controller leader election
    active_lock_id: int = Field(
        default=0x444C5743_414B5631,
        ge=1,
        le=9_223_372_036_854_775_807,   # PG bigint max (2**63 - 1)
    )  # 'DLWC AKV1'
    leader_poll_interval_seconds: float = Field(default=5.0, ge=0.5, le=60.0)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: all `test_config.py` tests pass (the pre-existing ones plus the 6 new ones).

- [ ] **Step 5: Commit**

```bash
git add src/dlw/config.py tests/test_config.py
git commit -m "feat(config): add active_lock_id + leader_poll_interval_seconds (W3c)"
```

---

### Task 2: `LeaderElector` — the PG-advisory-lock primitive

**Files:**
- Create: `src/dlw/services/leader_election.py`
- Test: `tests/services/test_leader_election.py` (create)

- [ ] **Step 1: Write the failing test file**

Create `tests/services/test_leader_election.py`:

```python
"""Tests for LeaderElector (Phase 2 W3c — PG advisory lock primitive)."""
from __future__ import annotations

import pytest

from dlw.services.leader_election import LeaderElector


def _db_url() -> str:
    """URL for the PostgreSQL admin DB used by these tests. Advisory locks
    are cluster-wide (not per-DB), so the choice of DB doesn't affect lock
    isolation — only the lock_id does. This file's _LOCK_ID is unique and
    pytest runs serially within a session, so collisions aren't a concern."""
    import os
    env = {
        "host": os.environ.get("DLW_TEST_PG_HOST", "localhost"),
        "port": os.environ.get("DLW_TEST_PG_PORT", "5433"),
        "user": os.environ.get("DLW_TEST_PG_USER", "postgres"),
        "password": os.environ.get("DLW_TEST_PG_PASSWORD", ""),
    }
    auth = f"{env['user']}:{env['password']}@" if env["password"] else f"{env['user']}@"
    return f"postgresql+asyncpg://{auth}{env['host']}:{env['port']}/postgres"


_LOCK_ID = 0x4C45_4145_4445_5231  # 'LEAD ER1'


@pytest.mark.slow
async def test_first_acquire_succeeds() -> None:
    """A fresh elector acquires the advisory lock."""
    e = LeaderElector(_db_url(), _LOCK_ID)
    try:
        assert await e.try_acquire() is True
    finally:
        await e.release()


@pytest.mark.slow
async def test_second_elector_cannot_acquire() -> None:
    """While elector A holds the lock, elector B (same lock_id) gets False."""
    a = LeaderElector(_db_url(), _LOCK_ID)
    b = LeaderElector(_db_url(), _LOCK_ID)
    try:
        assert await a.try_acquire() is True
        assert await b.try_acquire() is False
    finally:
        await b.release()
        await a.release()


@pytest.mark.slow
async def test_release_frees_lock_for_next_elector() -> None:
    """After A releases, B can acquire."""
    a = LeaderElector(_db_url(), _LOCK_ID)
    b = LeaderElector(_db_url(), _LOCK_ID)
    try:
        assert await a.try_acquire() is True
        await a.release()
        assert await b.try_acquire() is True
    finally:
        await b.release()


@pytest.mark.slow
async def test_connection_drop_frees_lock() -> None:
    """The crash-failover guarantee: if A's lock-holding connection is closed
    without an explicit unlock, PG releases the lock and B can acquire."""
    a = LeaderElector(_db_url(), _LOCK_ID)
    b = LeaderElector(_db_url(), _LOCK_ID)
    try:
        assert await a.try_acquire() is True
        # Simulate process death by closing the underlying connection directly,
        # bypassing release()'s polite pg_advisory_unlock.
        assert a._conn is not None
        await a._conn.close()
        a._conn = None
        # PG releases on session end; B should now acquire.
        assert await b.try_acquire() is True
    finally:
        await b.release()
        await a.release()


@pytest.mark.slow
async def test_verify_returns_true_when_holding() -> None:
    """verify() pings the connection and confirms it's alive."""
    e = LeaderElector(_db_url(), _LOCK_ID)
    try:
        assert await e.try_acquire() is True
        assert await e.verify() is True
    finally:
        await e.release()


@pytest.mark.slow
async def test_verify_returns_false_after_connection_drop() -> None:
    """verify() must exercise its exception branch — when _conn is still set
    but the underlying connection is dead, the SELECT 1 ping raises and
    verify() cleans up + returns False. (Pre-NULLing _conn would short-circuit
    on the `if self._conn is None: return False` guard and skip the path
    that matters in production.)"""
    e = LeaderElector(_db_url(), _LOCK_ID)
    try:
        assert await e.try_acquire() is True
        assert e._conn is not None
        await e._conn.close()
        # _conn intentionally LEFT non-None — verify() must hit the
        # SELECT-1-raises exception branch, not the early None-guard.
        assert await e.verify() is False
        # And cleanup happened — _conn is None now:
        assert e._conn is None
    finally:
        await e.release()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/services/test_leader_election.py -v`
Expected: 6 ImportError failures (`ModuleNotFoundError: No module named 'dlw.services.leader_election'`).

- [ ] **Step 3: Create the `LeaderElector` module**

Create `src/dlw/services/leader_election.py`:

```python
"""Controller leader election via PG session advisory lock (Phase 2 W3c).

The instance holding pg_try_advisory_lock(<active_lock_id>) is the active
controller. PG releases the lock the instant its holding session ends, which
gives us automatic failover with no lease/expiry logic. Single-shared-PG
design — PG HA is a separate concern (Phase 4)."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

logger = logging.getLogger(__name__)

ControllerState = Literal["standby", "recovering", "active"]


class LeaderElector:
    """Owns a dedicated PG connection that holds the advisory lock."""

    def __init__(self, db_url: str, lock_id: int) -> None:
        self._db_url = db_url
        self._lock_id = lock_id
        self._engine: AsyncEngine | None = None
        self._conn: AsyncConnection | None = None

    async def try_acquire(self) -> bool:
        """Try to acquire the lock. Returns True on success (and keeps the
        connection open holding it); False if another instance holds it."""
        if self._engine is None:
            # Dedicated engine. NullPool so SQLAlchemy never recycles the
            # connection out from under us (recycling would release the lock).
            # tcp_keepalives so a half-open TCP gets detected reasonably fast,
            # bounding the worst-case failover floor.
            self._engine = create_async_engine(
                self._db_url,
                poolclass=NullPool,
                connect_args={"server_settings": {
                    "tcp_keepalives_idle": "30",
                    "tcp_keepalives_interval": "10",
                }},
            )
        if self._conn is None:
            self._conn = await self._engine.connect()
        result = await self._conn.execute(
            text("SELECT pg_try_advisory_lock(:lock_id)"),
            {"lock_id": self._lock_id},
        )
        return bool(result.scalar())

    async def verify(self) -> bool:
        """Ping the lock-holding connection. Returns True if still alive
        (we still hold the lock); False if the connection died (lock lost)."""
        if self._conn is None:
            return False
        try:
            await self._conn.execute(text("SELECT 1"))
            return True
        except Exception as e:
            logger.warning("leader connection lost: %s", e)
            await self._cleanup_connection()
            return False

    async def release(self) -> None:
        """Explicit release on graceful shutdown. Closing the connection
        also releases — this is just the polite version."""
        if self._conn is not None:
            try:
                await self._conn.execute(
                    text("SELECT pg_advisory_unlock(:lock_id)"),
                    {"lock_id": self._lock_id},
                )
            except Exception as e:
                logger.warning("pg_advisory_unlock failed (releases on close anyway): %s", e)
        await self._cleanup_connection()
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    async def _cleanup_connection(self) -> None:
        if self._conn is not None:
            try:
                await self._conn.close()
            except Exception as exc:
                logger.debug("conn.close() during cleanup raised (ignored): %s", exc)
            self._conn = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/services/test_leader_election.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/dlw/services/leader_election.py tests/services/test_leader_election.py
git commit -m "feat(controller): LeaderElector — PG advisory-lock primitive (W3c)"
```

---

## Milestone 2 — Leader loop, health endpoint, barriers, lifespan

### Task 3: `run_leader_loop` — the state-machine coroutine

**Files:**
- Modify: `src/dlw/services/leader_election.py` (append `run_leader_loop`)
- Test: `tests/services/test_leader_loop.py` (create)

- [ ] **Step 1: Write the failing test file**

Create `tests/services/test_leader_loop.py`:

```python
"""Tests for run_leader_loop driven by fake callbacks (Phase 2 W3c)."""
from __future__ import annotations

import asyncio

import pytest


class _FakeElector:
    """Drives try_acquire/verify return values from a queue."""
    def __init__(self, acquires: list[bool], verifies: list[bool] | None = None) -> None:
        self._acquires = list(acquires)
        self._verifies = list(verifies or [])

    async def try_acquire(self) -> bool:
        return self._acquires.pop(0) if self._acquires else False

    async def verify(self) -> bool:
        return self._verifies.pop(0) if self._verifies else True

    async def release(self) -> None:
        pass


async def _run_for(loop_coro_factory, ticks: float) -> None:
    """Spawn the leader loop, let it run for `ticks` seconds, then signal
    shutdown and await clean exit."""
    shutdown = asyncio.Event()
    task = asyncio.create_task(loop_coro_factory(shutdown))
    await asyncio.sleep(ticks)
    shutdown.set()
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.slow
async def test_standby_polls_until_lock_available() -> None:
    """Standby retries try_acquire across ticks; on success, transitions
    through recovering → active and runs both callbacks exactly once."""
    from dlw.services.leader_election import run_leader_loop

    states: list[str] = []
    promote_calls = 0
    active_calls = 0

    async def on_promote() -> None:
        nonlocal promote_calls
        promote_calls += 1

    async def on_active() -> None:
        nonlocal active_calls
        active_calls += 1

    async def on_step_down() -> None:
        pass

    elector = _FakeElector(acquires=[False, False, True], verifies=[True] * 10)

    def loop_factory(shutdown):
        return run_leader_loop(
            elector=elector, poll_interval_seconds=0.05,
            set_state=states.append, on_promote=on_promote,
            on_active=on_active, on_step_down=on_step_down,
            shutdown=shutdown,
        )

    await _run_for(loop_factory, ticks=0.5)
    # Standby (initial) → standby (failed acquire 1) → standby (failed acquire 2)
    # → recovering → active. We see at minimum: ["standby", "recovering", "active"].
    assert "standby" in states
    assert "recovering" in states
    assert "active" in states
    assert states.index("recovering") < states.index("active")
    assert promote_calls == 1
    assert active_calls == 1


@pytest.mark.slow
async def test_recovery_failure_stays_in_recovering_and_retries() -> None:
    """on_promote raising keeps state at recovering; the loop retries next
    tick; once on_promote succeeds, state advances to active."""
    from dlw.services.leader_election import run_leader_loop

    states: list[str] = []
    promote_calls = 0

    async def on_promote() -> None:
        nonlocal promote_calls
        promote_calls += 1
        if promote_calls == 1:
            raise RuntimeError("recovery boom")
        # second call succeeds

    async def on_active() -> None:
        pass

    async def on_step_down() -> None:
        pass

    elector = _FakeElector(acquires=[True], verifies=[True] * 10)

    def loop_factory(shutdown):
        return run_leader_loop(
            elector=elector, poll_interval_seconds=0.05,
            set_state=states.append, on_promote=on_promote,
            on_active=on_active, on_step_down=on_step_down,
            shutdown=shutdown,
        )

    await _run_for(loop_factory, ticks=0.5)
    # First promote raises → stays recovering. State sequence must show
    # 'recovering' present and eventually 'active' after the 2nd promote.
    assert promote_calls >= 2
    assert states.count("recovering") >= 1
    assert "active" in states


@pytest.mark.slow
async def test_step_down_on_connection_loss() -> None:
    """When verify() returns False (lost connection) the loop calls
    on_step_down and reverts state to standby."""
    from dlw.services.leader_election import run_leader_loop

    states: list[str] = []
    step_down_calls = 0

    async def on_promote() -> None:
        pass

    async def on_active() -> None:
        pass

    async def on_step_down() -> None:
        nonlocal step_down_calls
        step_down_calls += 1

    # acquire → True; verify ticks: True, then False (connection died), then
    # the loop tries to re-acquire — fake elector returns False thereafter.
    elector = _FakeElector(acquires=[True, False, False], verifies=[True, False])

    def loop_factory(shutdown):
        return run_leader_loop(
            elector=elector, poll_interval_seconds=0.05,
            set_state=states.append, on_promote=on_promote,
            on_active=on_active, on_step_down=on_step_down,
            shutdown=shutdown,
        )

    await _run_for(loop_factory, ticks=0.5)
    assert step_down_calls >= 1
    # We must have transitioned: standby → recovering → active → standby
    # (last 'standby' is the step-down). Assert at least one re-entry to
    # standby AFTER 'active' appeared.
    first_active_idx = states.index("active")
    assert any(s == "standby" for s in states[first_active_idx + 1:])


@pytest.mark.slow
async def test_shutdown_event_exits_cleanly() -> None:
    """Setting the shutdown event causes the loop to exit within one tick."""
    from dlw.services.leader_election import run_leader_loop

    async def on_promote() -> None:
        pass

    async def on_active() -> None:
        pass

    async def on_step_down() -> None:
        pass

    elector = _FakeElector(acquires=[True], verifies=[True] * 100)
    states: list[str] = []
    shutdown = asyncio.Event()
    task = asyncio.create_task(run_leader_loop(
        elector=elector, poll_interval_seconds=0.05,
        set_state=states.append, on_promote=on_promote,
        on_active=on_active, on_step_down=on_step_down,
        shutdown=shutdown,
    ))
    await asyncio.sleep(0.1)
    shutdown.set()
    # Must exit within one poll interval (0.05s) + a small slack.
    await asyncio.wait_for(task, timeout=0.5)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/services/test_leader_loop.py -v`
Expected: 4 failures — `ImportError: cannot import name 'run_leader_loop' from 'dlw.services.leader_election'`.

- [ ] **Step 3: Append `run_leader_loop` to `leader_election.py`**

At the end of `src/dlw/services/leader_election.py`, append:

```python
async def run_leader_loop(
    *,
    elector: LeaderElector,
    poll_interval_seconds: float,
    set_state: Callable[[ControllerState], None],
    on_promote: Callable[[], Awaitable[None]],
    on_active: Callable[[], Awaitable[None]],
    on_step_down: Callable[[], Awaitable[None]],
    shutdown: asyncio.Event,
) -> None:
    """The leader loop. Stays in standby polling for the lock; on acquire
    transitions through recovering→active; on connection loss steps back to
    standby. Returns cleanly when `shutdown` is set."""
    state: ControllerState = "standby"
    set_state(state)
    while not shutdown.is_set():
        try:
            if state == "standby":
                if await elector.try_acquire():
                    state = "recovering"
                    set_state(state)
                    logger.info("leader: acquired lock, running recovery")
                    try:
                        await on_promote()
                    except Exception:
                        logger.exception("leader: recovery failed; will retry next tick")
                        # Stay in `recovering` — heartbeats keep 503ing.
                        # Don't release the lock (another instance can't fix it).
                        await _sleep_or_shutdown(shutdown, poll_interval_seconds)
                        continue
                    state = "active"
                    set_state(state)
                    try:
                        await on_active()
                    except Exception:
                        logger.exception("leader: on_active failed; reverting to recovering for retry")
                        state = "recovering"
                        set_state(state)
                        await _sleep_or_shutdown(shutdown, poll_interval_seconds)
                        continue
                    logger.info("leader: promoted to active")
            elif state == "recovering":
                # Lock is held but promotion hasn't completed. Verify lock still
                # alive, then retry on_promote.
                if not await elector.verify():
                    logger.warning("leader: lost lock during recovery, stepping down")
                    await on_step_down()
                    state = "standby"
                    set_state(state)
                    continue
                try:
                    await on_promote()
                except Exception:
                    logger.exception("leader: recovery failed; will retry next tick")
                    await _sleep_or_shutdown(shutdown, poll_interval_seconds)
                    continue
                state = "active"
                set_state(state)
                try:
                    await on_active()
                except Exception:
                    logger.exception("leader: on_active failed; reverting to recovering for retry")
                    state = "recovering"
                    set_state(state)
                    await _sleep_or_shutdown(shutdown, poll_interval_seconds)
                    continue
                logger.info("leader: promoted to active (after retry)")
            elif state == "active":
                if not await elector.verify():
                    logger.warning("leader: lost lock, stepping down to standby")
                    await on_step_down()
                    state = "standby"
                    set_state(state)
                    continue
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("leader loop iteration failed")
        await _sleep_or_shutdown(shutdown, poll_interval_seconds)


async def _sleep_or_shutdown(shutdown: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(shutdown.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/services/test_leader_loop.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/dlw/services/leader_election.py tests/services/test_leader_loop.py
git commit -m "feat(controller): run_leader_loop state machine (W3c)"
```

---

### Task 4: `make_app_with_state` conftest helper + migrate 7 existing fixture sites

**Files:**
- Modify: `tests/conftest.py` (append helper)
- Modify: `tests/api/test_executors.py`, `test_register_endpoint.py`, `test_renew_endpoint.py`, `test_subtasks.py`, `test_hf_proxy.py`, `tests/e2e/test_executor_e2e.py`, `tests/e2e/test_happy_path.py`

- [ ] **Step 1: Add the helper to conftest**

Append to `tests/conftest.py` (at the bottom of the file, after `make_fake_controller_client`):

```python
def make_app_with_state(
    ephemeral_ca,
    *,
    enrollment_token: str,
    controller_state: str = "active",
):
    """Build a controller app for ASGI-transport tests with app.state pre-seeded
    (skips the lifespan bootstrap). Defaults controller_state to 'active' so
    the W3c recovery barrier doesn't fire."""
    from dlw.auth.hmac_nonce import NonceStore
    from dlw.main import create_app
    app = create_app()
    app.state.ca = ephemeral_ca["ca"]
    app.state.jwt_keypair = ephemeral_ca["jwt_keypair"]
    app.state.nonce_store = NonceStore(maxsize=1000, ttl_seconds=300)
    app.state.enrollment_token = enrollment_token
    app.state.controller_state = controller_state
    return app
```

- [ ] **Step 2: Migrate `tests/api/test_executors.py`**

In `tests/api/test_executors.py`, find the `client` fixture (around line 60-70). Currently:

```python
@pytest.fixture
async def client(ephemeral_ca):
    from dlw.main import create_app
    from dlw.auth.hmac_nonce import NonceStore
    app = create_app()
    app.state.ca = ephemeral_ca["ca"]
    app.state.jwt_keypair = ephemeral_ca["jwt_keypair"]
    app.state.nonce_store = NonceStore(maxsize=1000, ttl_seconds=300)
    app.state.enrollment_token = _ENROLL
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
```

Replace with:

```python
@pytest.fixture
async def client(ephemeral_ca):
    from tests.conftest import make_app_with_state
    app = make_app_with_state(ephemeral_ca, enrollment_token=_ENROLL)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
```

- [ ] **Step 3: Migrate `tests/api/test_register_endpoint.py`** (lines ~40-49)

Replace the inline app setup with one call to `make_app_with_state(ephemeral_ca, enrollment_token=_ENROLL)`. Drop the `from dlw.main import create_app` and `from dlw.auth.hmac_nonce import NonceStore` imports from the fixture (they're no longer needed there). Same surrounding `AsyncClient(transport=ASGITransport(app=app), base_url="http://test")` wrapper.

- [ ] **Step 4: Migrate `tests/api/test_renew_endpoint.py`** (lines ~38-49)

Same pattern as Step 3.

- [ ] **Step 5: Migrate `tests/api/test_subtasks.py`** (lines ~70-78)

Same pattern.

- [ ] **Step 6: Migrate `tests/api/test_hf_proxy.py`** (the `proxy_app` fixture, lines ~70-76)

Currently:
```python
@pytest.fixture
def proxy_app(ephemeral_ca):
    from dlw.auth.hmac_nonce import NonceStore
    from dlw.main import create_app
    app = create_app()
    app.state.ca = ephemeral_ca["ca"]
    app.state.jwt_keypair = ephemeral_ca["jwt_keypair"]
    app.state.nonce_store = NonceStore(maxsize=1000, ttl_seconds=300)
    app.state.enrollment_token = _ENROLL
    return app
```

Replace with:
```python
@pytest.fixture
def proxy_app(ephemeral_ca):
    from tests.conftest import make_app_with_state
    return make_app_with_state(ephemeral_ca, enrollment_token=_ENROLL)
```

- [ ] **Step 7: Migrate `tests/e2e/test_executor_e2e.py`** (lines ~125-134)

The e2e test constructs the app inline mid-test. Find:
```python
        from dlw.main import create_app
        from dlw.auth.hmac_nonce import NonceStore
        from dlw.executor.auth_lifecycle import AuthState
        from tests.conftest import register_test_executor
        app = create_app()
        # W3a: inject the auth substrate onto app.state (skip the lifespan
        # bootstrap — this test drives the ASGI app directly, no real server).
        app.state.ca = ephemeral_ca["ca"]
        app.state.jwt_keypair = ephemeral_ca["jwt_keypair"]
        app.state.nonce_store = NonceStore(maxsize=1000, ttl_seconds=300)
        app.state.enrollment_token = _ENROLL
        asgi_transport = httpx.ASGITransport(app=app)
```

Replace with:
```python
        from dlw.executor.auth_lifecycle import AuthState
        from tests.conftest import make_app_with_state, register_test_executor
        # W3a: app.state injected by helper (skip the lifespan bootstrap).
        app = make_app_with_state(ephemeral_ca, enrollment_token=_ENROLL)
        asgi_transport = httpx.ASGITransport(app=app)
```

- [ ] **Step 8: Migrate `tests/e2e/test_happy_path.py`** (lines ~55-65)

Same pattern: replace the inline `app = create_app()` + 4-line `app.state.*` block with `app = make_app_with_state(ephemeral_ca, enrollment_token=_ENROLL)`.

- [ ] **Step 9: Run the affected test suites to verify everything still passes**

Run: `uv run pytest tests/api/test_executors.py tests/api/test_register_endpoint.py tests/api/test_renew_endpoint.py tests/api/test_subtasks.py tests/api/test_hf_proxy.py tests/e2e/test_happy_path.py tests/e2e/test_executor_e2e.py -v 2>&1 | tail -20`
Expected: PASS — all pre-existing tests pass (the helper sets `controller_state="active"` by default, identical effective behavior).

- [ ] **Step 10: Commit**

```bash
git add tests/conftest.py tests/api/test_executors.py tests/api/test_register_endpoint.py \
        tests/api/test_renew_endpoint.py tests/api/test_subtasks.py tests/api/test_hf_proxy.py \
        tests/e2e/test_executor_e2e.py tests/e2e/test_happy_path.py
git commit -m "test: make_app_with_state helper + migrate 7 fixture sites (W3c)"
```

---

### Task 5: `GET /health/active` endpoint

**Files:**
- Modify: `src/dlw/api/health.py`
- Test: `tests/api/test_health_active.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/api/test_health_active.py`:

```python
"""Tests for GET /health/active (Phase 2 W3c — LB target endpoint)."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def app_factory(ephemeral_ca):
    from tests.conftest import make_app_with_state
    def _make(controller_state: str):
        return make_app_with_state(
            ephemeral_ca, enrollment_token="ignored", controller_state=controller_state,
        )
    return _make


@pytest.mark.slow
async def test_health_active_503_when_standby(app_factory) -> None:
    app = app_factory("standby")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/health/active")
    assert r.status_code == 503
    assert r.json()["detail"]["controller_state"] == "standby"


@pytest.mark.slow
async def test_health_active_200_when_recovering(app_factory) -> None:
    app = app_factory("recovering")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/health/active")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "active"
    assert body["controller_state"] == "recovering"


@pytest.mark.slow
async def test_health_active_200_when_active(app_factory) -> None:
    app = app_factory("active")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/health/active")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "active"
    assert body["controller_state"] == "active"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/api/test_health_active.py -v`
Expected: 3 failures — `404 Not Found` (route doesn't exist yet).

- [ ] **Step 3: Add the endpoint**

In `src/dlw/api/health.py`, add `Request` to the FastAPI imports and append a new route. Replace:

```python
from fastapi import APIRouter, HTTPException
```

with:

```python
from fastapi import APIRouter, HTTPException, Request
```

Then at the end of the file (after the `/ready` route), append:

```python
@router.get("/active")
async def active(request: Request) -> dict[str, str]:
    """W3c: LB target — 200 iff this instance holds the leader lock
    (controller_state in {'recovering', 'active'}). 503 otherwise."""
    state = getattr(request.app.state, "controller_state", "standby")
    if state in ("recovering", "active"):
        return {"status": "active", "controller_state": state}
    raise HTTPException(status_code=503, detail={"controller_state": state})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/api/test_health_active.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/dlw/api/health.py tests/api/test_health_active.py
git commit -m "feat(api): GET /health/active LB target (W3c)"
```

---

### Task 6: `_recovery_barrier.py` dep + wire into 3 executor-loop routes + tests

**Files:**
- Create: `src/dlw/api/_recovery_barrier.py`
- Modify: `src/dlw/api/executors.py` (heartbeat + poll routes)
- Modify: `src/dlw/api/subtasks.py` (report route)
- Test: `tests/api/test_recovery_barrier.py` (create)
- Test: `tests/executor/test_client.py` (append 1 case)

- [ ] **Step 1: Write the failing tests**

Create `tests/api/test_recovery_barrier.py`:

```python
"""Tests for require_not_recovering dep — 503 barrier on executor-loop endpoints
during controller recovery (Phase 2 W3c, INVARIANT 33)."""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.db.base import Base


_ENROLL = "test-enroll-barrier"


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(Tenant(id=1, slug="d", display_name="D"))
        await s.flush()
        s.add(Project(id=1, tenant_id=1, name="d"))
        s.add(User(id=1, tenant_id=1, oidc_subject="d",
                   email="d@l", role="tenant_admin"))
        s.add(StorageBackend(id=1, tenant_id=1, name="d",
                             backend_type="s3", config_encrypted=b""))
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch):
    from dlw.config import get_settings
    monkeypatch.setenv("DLW_TLS_TRUSTED_PROXY", "1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.slow
async def test_heartbeat_503_when_recovering(ephemeral_ca) -> None:
    from tests.conftest import (
        make_app_with_state, register_test_executor, signed_heartbeat_headers,
    )
    app = make_app_with_state(
        ephemeral_ca, enrollment_token=_ENROLL, controller_state="recovering",
    )
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        reg = await register_test_executor(
            c, enrollment_token=_ENROLL,
            executor_id="bar-worker-1", host_id="bar-host",
        )
        hb_body = b'{"health_score": 100, "parts_dir_bytes": 0, "disk_free_gb": 100}'
        r = await c.post("/api/v1/executors/bar-worker-1/heartbeat",
                         content=hb_body,
                         headers=signed_heartbeat_headers(reg, hb_body))
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "CONTROLLER_RECOVERING"


@pytest.mark.slow
async def test_poll_and_report_also_503_when_recovering(ephemeral_ca) -> None:
    from tests.conftest import (
        executor_request_headers, make_app_with_state, register_test_executor,
    )
    app = make_app_with_state(
        ephemeral_ca, enrollment_token=_ENROLL, controller_state="recovering",
    )
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        reg = await register_test_executor(
            c, enrollment_token=_ENROLL,
            executor_id="bar-worker-2", host_id="bar-host",
        )
        # /poll → 503
        r = await c.post("/api/v1/executors/bar-worker-2/poll",
                         headers=executor_request_headers(reg))
        assert r.status_code == 503
        assert r.json()["detail"]["code"] == "CONTROLLER_RECOVERING"
        # /report → 503
        r = await c.post(f"/api/v1/subtasks/{uuid.uuid4()}/report", json={
            "status": "succeeded", "actual_sha256": "f" * 64,
            "bytes_downloaded": 0,
        }, headers=executor_request_headers(reg))
        assert r.status_code == 503
        assert r.json()["detail"]["code"] == "CONTROLLER_RECOVERING"


@pytest.mark.slow
async def test_heartbeat_passes_when_active(ephemeral_ca) -> None:
    """Sanity: with controller_state='active', the barrier does not fire."""
    from tests.conftest import (
        make_app_with_state, register_test_executor, signed_heartbeat_headers,
    )
    app = make_app_with_state(
        ephemeral_ca, enrollment_token=_ENROLL, controller_state="active",
    )
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        reg = await register_test_executor(
            c, enrollment_token=_ENROLL,
            executor_id="bar-worker-3", host_id="bar-host",
        )
        hb_body = b'{"health_score": 100, "parts_dir_bytes": 0, "disk_free_gb": 100}'
        r = await c.post("/api/v1/executors/bar-worker-3/heartbeat",
                         content=hb_body,
                         headers=signed_heartbeat_headers(reg, hb_body))
    assert r.status_code == 200
```

Append to `tests/executor/test_client.py`:

```python
@pytest.mark.slow
async def test_controller_recovering_503_is_retried_by_tenacity(tmp_path) -> None:
    """W3c: a 503 with detail.code=CONTROLLER_RECOVERING from the controller is
    a transient response. The existing tenacity _retry on ControllerClient
    retries it; by the second attempt the controller is active and returns 200."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(503, json={
                "detail": {"code": "CONTROLLER_RECOVERING",
                           "message": "controller recovering after failover"},
            })
        return httpx.Response(200, json={
            "id": "ex-recov", "status": "healthy", "health_score": 100,
        })

    state = make_fake_auth_state(
        tmp_path, executor_id="ex-recov", epoch=1, jwt="jwt-recov",
        hmac_seed=_HMAC_SEED,
    )
    async with ControllerClient(
        base_url="http://test", auth_state=state,
        _transport=httpx.MockTransport(handler),
    ) as c:
        r = await c.heartbeat(executor_id="ex-recov", health_score=100,
                              parts_dir_bytes=0)
    assert r["status"] == "healthy"
    assert call_count == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/api/test_recovery_barrier.py tests/executor/test_client.py::test_controller_recovering_503_is_retried_by_tenacity -v`
Expected: barrier tests pass `test_heartbeat_passes_when_active` but FAIL `test_heartbeat_503_when_recovering` / `test_poll_and_report_also_503_when_recovering` (the barrier dep doesn't exist yet, so endpoints proceed and return 200/4xx not 503). The executor-client test should already PASS — the existing tenacity `_retry` already retries `HTTPStatusError` 5xx, so a 503 followed by 200 succeeds on second attempt. (If it fails, it indicates tenacity coverage is narrower than expected — investigate before continuing.)

- [ ] **Step 3: Create the barrier dependency**

Create `src/dlw/api/_recovery_barrier.py`:

```python
"""FastAPI dep that 503s executor-loop calls while the controller is recovering
after a failover (Phase 2 W3c, INVARIANT 33)."""
from __future__ import annotations

from fastapi import HTTPException, Request


async def require_not_recovering(request: Request) -> None:
    """Returns normally if the controller is active (or state is unset, which
    defaults to active for tests bypassing the lifespan). Raises 503 with
    detail.code='CONTROLLER_RECOVERING' if state == 'recovering'."""
    state = getattr(request.app.state, "controller_state", "active")
    if state == "recovering":
        raise HTTPException(
            status_code=503,
            detail={"code": "CONTROLLER_RECOVERING",
                    "message": "controller recovering after failover, retry shortly"},
        )
```

- [ ] **Step 4: Wire the dep into the heartbeat and poll routes**

In `src/dlw/api/executors.py`, add the import:

```python
from dlw.api._recovery_barrier import require_not_recovering
```

(Place it near the other `from dlw.auth...` and `from dlw.api...` imports.)

Then add the dep to the heartbeat route. Find:

```python
@router.post("/{executor_id}/heartbeat")
async def post_heartbeat(
    body: ExecutorHeartbeat,
    _hmac: Executor = Depends(require_hmac_heartbeat),
    executor: Executor = Depends(require_executor_epoch),
    session: AsyncSession = Depends(_session),
) -> ExecutorRead:
```

Replace with:

```python
@router.post("/{executor_id}/heartbeat",
             dependencies=[Depends(require_not_recovering)])
async def post_heartbeat(
    body: ExecutorHeartbeat,
    _hmac: Executor = Depends(require_hmac_heartbeat),
    executor: Executor = Depends(require_executor_epoch),
    session: AsyncSession = Depends(_session),
) -> ExecutorRead:
```

Same change for the poll route — find:

```python
@router.post("/{executor_id}/poll")
async def post_poll(
    executor: Executor = Depends(require_executor_epoch),
    session: AsyncSession = Depends(_session),
) -> AssignmentResponse:
```

Replace with:

```python
@router.post("/{executor_id}/poll",
             dependencies=[Depends(require_not_recovering)])
async def post_poll(
    executor: Executor = Depends(require_executor_epoch),
    session: AsyncSession = Depends(_session),
) -> AssignmentResponse:
```

- [ ] **Step 5: Wire the dep into the report route**

In `src/dlw/api/subtasks.py`, add the import:

```python
from dlw.api._recovery_barrier import require_not_recovering
```

Then find:

```python
@router.post("/{subtask_id}/report")
async def post_report(
```

Replace with:

```python
@router.post("/{subtask_id}/report",
             dependencies=[Depends(require_not_recovering)])
async def post_report(
```

- [ ] **Step 6: Run the barrier tests + the broader executor/subtasks suites**

Run: `uv run pytest tests/api/test_recovery_barrier.py tests/api/test_executors.py tests/api/test_subtasks.py tests/executor/test_client.py -v 2>&1 | tail -20`
Expected: all PASS — the new barrier tests pass; pre-existing `test_executors.py` and `test_subtasks.py` keep passing because their fixtures use `make_app_with_state(..., controller_state="active")` (which is the helper's default — applied in Task 4).

- [ ] **Step 7: Commit**

```bash
git add src/dlw/api/_recovery_barrier.py src/dlw/api/executors.py src/dlw/api/subtasks.py \
        tests/api/test_recovery_barrier.py tests/executor/test_client.py
git commit -m "feat(api): require_not_recovering 503 barrier on executor-loop endpoints (W3c)"
```

---

### Task 7: `main.py` lifespan restructure — leader-gate recovery + sweep

**Files:**
- Modify: `src/dlw/main.py`

This task has no new tests — the leader loop is tested separately (T2/T3), and the lifespan's behavior is observable end-to-end in the chaos drill (T8). What this task DOES is ensure pre-existing tests keep passing under the new lifespan structure.

- [ ] **Step 1: Restructure `lifespan`**

In `src/dlw/main.py`, replace the entire `lifespan` function body. Current `lifespan` runs `run_recovery_routine` unconditionally before serving + spawns `_sweep_loop_main` unconditionally. The new version starts the leader loop and lets it gate both.

Find the `lifespan` function (lines 19-96) and replace it entirely with:

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """W3c: leader-gated lifespan. The W3a auth substrate is bootstrapped
    unconditionally (both roles need it ready so promotion is instant). The
    run_recovery_routine + sweep loop are started by the leader loop only
    after this instance acquires the leader advisory lock."""
    from dlw.db.session import get_engine, reset_engine
    from dlw.services.leader_election import LeaderElector, run_leader_loop
    from dlw.services.recovery import run_recovery_routine

    factory = async_sessionmaker(get_engine(), expire_on_commit=False)

    # W3a auth bootstrap — UNCHANGED. Both active and standby need this ready.
    from dlw.auth.uvicorn_tls_patch import install_transport_scope_patch
    install_transport_scope_patch()

    from pathlib import Path
    from dlw.auth.ca import bootstrap_ca, ensure_server_cert
    from dlw.auth.jwt_signing import bootstrap_keypair
    from dlw.auth.hmac_nonce import NonceStore
    import secrets as _secrets
    from dlw.config import get_settings as _gs
    _settings = _gs()
    _ca_dir = Path(_settings.ca_dir)
    _ca_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    _ca = bootstrap_ca(_ca_dir)
    ensure_server_cert(_ca, _ca_dir, hostname=_settings.controller_hostname)
    _jwt_kp = bootstrap_keypair(_ca_dir)
    if _settings.enrollment_token:
        _enroll = _settings.enrollment_token
    else:
        _tok_path = _ca_dir / "enrollment.token"
        if _tok_path.exists():
            _enroll = _tok_path.read_text().strip()
        else:
            _enroll = _secrets.token_hex(32)
            _tok_path.write_text(_enroll)
            _tok_path.chmod(0o600)
            logger.info("generated enrollment token (copy to executors): %s", _enroll)
    app.state.ca = _ca
    app.state.jwt_keypair = _jwt_kp
    app.state.nonce_store = NonceStore(maxsize=10_000, ttl_seconds=300)
    app.state.enrollment_token = _enroll

    # W3c: controller state + leader loop.
    app.state.controller_state = "standby"
    shutdown = asyncio.Event()
    elector = LeaderElector(
        db_url=_settings.db_url, lock_id=_settings.active_lock_id,
    )
    sweep_task_holder: dict[str, asyncio.Task | None] = {"t": None}

    def _set_state(s: str) -> None:
        app.state.controller_state = s

    async def _on_promote() -> None:
        async with factory() as session:
            stats = await run_recovery_routine(session)
            await session.commit()
            logger.info("recovery on promote: %s", stats.as_dict())

    async def _on_active() -> None:
        sweep_task_holder["t"] = asyncio.create_task(_sweep_loop_main(factory))

    async def _on_step_down() -> None:
        t = sweep_task_holder["t"]
        if t is not None:
            t.cancel()
            try:
                await asyncio.wait_for(t, timeout=2)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            sweep_task_holder["t"] = None

    leader_task = asyncio.create_task(run_leader_loop(
        elector=elector,
        poll_interval_seconds=_settings.leader_poll_interval_seconds,
        set_state=_set_state,
        on_promote=_on_promote,
        on_active=_on_active,
        on_step_down=_on_step_down,
        shutdown=shutdown,
    ))
    try:
        yield
    finally:
        shutdown.set()
        await _on_step_down()
        try:
            await asyncio.wait_for(leader_task, timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            leader_task.cancel()
        await elector.release()
        await reset_engine()
```

The `_sweep_loop_main` function below `lifespan` stays exactly as-is.

The `DLW_STRICT_RECOVERY` env knob — and its `import os` inside the lifespan — are deleted by this replacement. (The leader loop's "stay in recovering, retry next tick on failure" replaces it.)

- [ ] **Step 2: Verify nothing references `DLW_STRICT_RECOVERY` elsewhere**

Run: `git grep -n "DLW_STRICT_RECOVERY\|strict_recovery"`
Expected: no hits in `src/` after this edit. If there are hits in `tests/` (a test that monkeypatches the env), update or remove them — they no longer affect anything.

- [ ] **Step 3: Run the full suite to verify nothing regressed**

Run: `uv run pytest -q 2>&1 | tail -10`
Expected: PASS (test count unchanged from before this task). Tests that use `ASGITransport` bypass the lifespan entirely, so the restructure has no visible effect on them. Tests that hit `/health/active` (Task 5) or the barrier (Task 6) work via the helper-injected `app.state.controller_state`.

- [ ] **Step 4: Commit**

```bash
git add src/dlw/main.py
git commit -m "feat(controller): lifespan leader-gates recovery + sweep, delete STRICT_RECOVERY (W3c)"
```

---

## Milestone 3 — Chaos drill, docs, PR

### Task 8: `test_failover_drill.py` + OpenAPI + operator runbook + PR

**Files:**
- Create: `tests/e2e/test_failover_drill.py`
- Modify: `api/openapi.yaml`
- Modify: `docs/operator/executor-runbook.md`

- [ ] **Step 1: Write the failover drill integration test**

Create `tests/e2e/test_failover_drill.py`:

```python
"""W3c chaos drill: two LeaderElector instances + two run_leader_loop tasks
against the same test PG. Simulate active death; assert the standby acquires
the lock and transitions standby → recovering → active end-to-end."""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.db.base import Base
from dlw.services.leader_election import LeaderElector, run_leader_loop


def _test_db_url(test_db_name: str) -> str:
    env = {
        "host": os.environ.get("DLW_TEST_PG_HOST", "localhost"),
        "port": os.environ.get("DLW_TEST_PG_PORT", "5433"),
        "user": os.environ.get("DLW_TEST_PG_USER", "postgres"),
        "password": os.environ.get("DLW_TEST_PG_PASSWORD", ""),
    }
    auth = f"{env['user']}:{env['password']}@" if env["password"] else f"{env['user']}@"
    return f"postgresql+asyncpg://{auth}{env['host']}:{env['port']}/{test_db_name}"


_LOCK_ID = 0x4D45_4145_5044_5631   # 'MEAPDV1' — distinct from test_leader_election


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    """Tables only — recovery_routine reconciles against real schema."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.mark.slow
async def test_two_instances_one_active(test_db_name) -> None:
    """Both leader loops start simultaneously; PG guarantees exactly one
    reaches 'active'; the other stays 'standby'."""
    db_url = _test_db_url(test_db_name)
    states_a: list[str] = []
    states_b: list[str] = []

    async def noop_promote(): pass
    async def noop_active(): pass
    async def noop_step_down(): pass

    elector_a = LeaderElector(db_url, _LOCK_ID)
    elector_b = LeaderElector(db_url, _LOCK_ID)
    shutdown = asyncio.Event()

    task_a = asyncio.create_task(run_leader_loop(
        elector=elector_a, poll_interval_seconds=0.05,
        set_state=states_a.append, on_promote=noop_promote,
        on_active=noop_active, on_step_down=noop_step_down,
        shutdown=shutdown,
    ))
    task_b = asyncio.create_task(run_leader_loop(
        elector=elector_b, poll_interval_seconds=0.05,
        set_state=states_b.append, on_promote=noop_promote,
        on_active=noop_active, on_step_down=noop_step_down,
        shutdown=shutdown,
    ))
    try:
        await asyncio.sleep(0.5)
        # One reached 'active', the other is stuck in 'standby'.
        a_active = states_a and states_a[-1] == "active"
        b_active = states_b and states_b[-1] == "active"
        assert (a_active or b_active) and not (a_active and b_active), \
            f"expected exactly one active: states_a={states_a}, states_b={states_b}"
    finally:
        shutdown.set()
        await asyncio.gather(task_a, task_b, return_exceptions=True)
        await elector_a.release()
        await elector_b.release()


@pytest.mark.slow
async def test_failover_promotes_standby_within_rto(test_db_name) -> None:
    """Active dies (loop cancelled + elector connection closed). Within
    ≤ 3 × poll_interval, the standby goes standby → recovering → active."""
    db_url = _test_db_url(test_db_name)
    states_a: list[str] = []
    states_b: list[str] = []
    poll_interval = 0.05

    async def noop_promote(): pass
    async def noop_active(): pass
    async def noop_step_down(): pass

    elector_a = LeaderElector(db_url, _LOCK_ID)
    elector_b = LeaderElector(db_url, _LOCK_ID)
    shutdown_a = asyncio.Event()
    shutdown_b = asyncio.Event()

    task_a = asyncio.create_task(run_leader_loop(
        elector=elector_a, poll_interval_seconds=poll_interval,
        set_state=states_a.append, on_promote=noop_promote,
        on_active=noop_active, on_step_down=noop_step_down,
        shutdown=shutdown_a,
    ))
    task_b = asyncio.create_task(run_leader_loop(
        elector=elector_b, poll_interval_seconds=poll_interval,
        set_state=states_b.append, on_promote=noop_promote,
        on_active=noop_active, on_step_down=noop_step_down,
        shutdown=shutdown_b,
    ))
    try:
        # Let A become active.
        await asyncio.sleep(0.3)
        assert states_a[-1] == "active", \
            f"A should be active by now: states_a={states_a}"
        assert states_b[-1] == "standby", \
            f"B should still be standby: states_b={states_b}"

        # Kill A: shut down its loop AND abruptly close its lock-holding
        # connection (simulating crash, not graceful release).
        shutdown_a.set()
        await asyncio.wait_for(task_a, timeout=1.0)
        if elector_a._conn is not None:
            await elector_a._conn.close()
            elector_a._conn = None

        # Within ≤ 3 × poll_interval B should pick up the lock and promote.
        await asyncio.sleep(poll_interval * 6)
        assert "recovering" in states_b, \
            f"B never entered recovering: states_b={states_b}"
        assert states_b[-1] == "active", \
            f"B did not reach active: states_b={states_b}"
    finally:
        shutdown_b.set()
        await asyncio.gather(task_b, return_exceptions=True)
        await elector_a.release()
        await elector_b.release()


@pytest.mark.slow
async def test_promoted_standby_runs_recovery_callback(test_db_name) -> None:
    """The on_promote callback actually runs on the standby's promotion path —
    proves run_recovery_routine would execute. Uses a counter callback, not
    the real recovery routine (which is tested in tests/services/test_recovery.py)."""
    db_url = _test_db_url(test_db_name)
    poll_interval = 0.05

    promote_a_count = 0
    promote_b_count = 0

    async def promote_a():
        nonlocal promote_a_count
        promote_a_count += 1

    async def promote_b():
        nonlocal promote_b_count
        promote_b_count += 1

    async def noop_active(): pass
    async def noop_step_down(): pass

    states_a: list[str] = []
    states_b: list[str] = []

    elector_a = LeaderElector(db_url, _LOCK_ID)
    elector_b = LeaderElector(db_url, _LOCK_ID)
    shutdown_a = asyncio.Event()
    shutdown_b = asyncio.Event()

    task_a = asyncio.create_task(run_leader_loop(
        elector=elector_a, poll_interval_seconds=poll_interval,
        set_state=states_a.append, on_promote=promote_a,
        on_active=noop_active, on_step_down=noop_step_down,
        shutdown=shutdown_a,
    ))
    task_b = asyncio.create_task(run_leader_loop(
        elector=elector_b, poll_interval_seconds=poll_interval,
        set_state=states_b.append, on_promote=promote_b,
        on_active=noop_active, on_step_down=noop_step_down,
        shutdown=shutdown_b,
    ))
    try:
        await asyncio.sleep(0.3)
        # One of A/B has promoted. Identify the dead-active path.
        if states_a[-1] == "active":
            assert promote_a_count == 1
            shutdown_a.set()
            await asyncio.wait_for(task_a, timeout=1.0)
            if elector_a._conn is not None:
                await elector_a._conn.close()
                elector_a._conn = None
            await asyncio.sleep(poll_interval * 6)
            assert promote_b_count == 1, \
                f"B's on_promote did not run after A died: {promote_b_count=}"
        else:
            assert promote_b_count == 1
            shutdown_b.set()
            await asyncio.wait_for(task_b, timeout=1.0)
            if elector_b._conn is not None:
                await elector_b._conn.close()
                elector_b._conn = None
            await asyncio.sleep(poll_interval * 6)
            assert promote_a_count == 1, \
                f"A's on_promote did not run after B died: {promote_a_count=}"
    finally:
        shutdown_a.set()
        shutdown_b.set()
        await asyncio.gather(task_a, task_b, return_exceptions=True)
        await elector_a.release()
        await elector_b.release()
```

- [ ] **Step 2: Run the failover drill to verify it passes**

Run: `uv run pytest tests/e2e/test_failover_drill.py -v`
Expected: 3 passed. If `test_failover_promotes_standby_within_rto` flakes (timing-sensitive — the 6×poll-interval window can be tight on a slow machine), bump the wait to `poll_interval * 10` rather than fundamentally changing the test.

- [ ] **Step 3: Document `/health/active` and the 503 in OpenAPI**

First READ `api/openapi.yaml` to learn its structure. Then:

(a) Add `/health/active` under `paths:` (near the existing `/health/live` and `/health/ready` entries — search for those to find the right location). Match the file's indentation:

```yaml
  /health/active:
    get:
      tags: [health]
      summary: LB target — 200 iff this instance holds the leader advisory lock
      operationId: healthActive
      responses:
        '200':
          description: Active or recovering — LB should route to this instance.
          content:
            application/json:
              schema:
                type: object
                properties:
                  status: { type: string, enum: [active] }
                  controller_state: { type: string, enum: [recovering, active] }
        '503':
          description: Standby — LB must NOT route to this instance.
          content:
            application/json:
              schema:
                type: object
                properties:
                  detail:
                    type: object
                    properties:
                      controller_state: { type: string, enum: [standby] }
```

(b) Add a `503 CONTROLLER_RECOVERING` response on the three executor-loop operations. Find the existing operations for `/api/v1/executors/{executorId}/heartbeat`, `/api/v1/executors/{executorId}/poll`, and `/api/v1/subtasks/{subtaskId}/report`. Each has a `responses:` block; add this entry to each:

```yaml
        '503':
          description: Controller is recovering after a failover (INVARIANT 33). Retry shortly.
          content:
            application/json:
              schema:
                type: object
                properties:
                  detail:
                    type: object
                    properties:
                      code: { type: string, enum: [CONTROLLER_RECOVERING] }
                      message: { type: string }
```

- [ ] **Step 4: Verify the OpenAPI spec lints clean**

Find the spectral CI command (`.github/workflows/`) and run it; otherwise run `npx --yes @stoplight/spectral-cli lint api/openapi.yaml`.
Expected: 0 NEW errors. Pre-existing warnings on other operations are acceptable.

- [ ] **Step 5: Update the operator runbook**

In `docs/operator/executor-runbook.md`, append a new section (match the existing heading levels — likely `##`):

```markdown
## Controller leadership (W3c)

As of Phase 2 W3c, the controller supports active/standby deployments via an
app-level leader election. The instance holding a session-level
PostgreSQL advisory lock (`pg_try_advisory_lock(<DLW_ACTIVE_LOCK_ID>)`) is
the **active**; all others are **standby**.

**LB routing:** point the load balancer's health check at `GET /health/active`
(returns 200 only when this instance holds the lock). `/health/live` and
`/health/ready` remain the k8s liveness/readiness probes — unchanged.

**Failover behaviour:** when the active dies, PG auto-releases the advisory
lock the instant its holding session ends. A standby's leader-loop poll
(default 5 s, configurable via `DLW_LEADER_POLL_INTERVAL_SECONDS`) acquires
the freed lock and promotes through `standby → recovering → active`. During
the `recovering` phase the executor-loop endpoints (heartbeat, poll, report)
return **503 `CONTROLLER_RECOVERING`** — executors retry through their
existing tenacity backoff. Total RTO target: ≤ 10 min.

**Relationship to PG-level failover (`promote-standby.sh`):** the app-level
lock is orthogonal to PostgreSQL primary failover. The runbook
`deploy/runbooks/scripts/promote-standby.sh` promotes the PG primary itself
(CH-Q3); after that script runs, the controller pods reconnect and the
advisory lock is re-acquired automatically by whichever pod wins the race.

**Required environment variables:**

- `DLW_ACTIVE_LOCK_ID` — bigint advisory-lock key. Default
  `0x444C5743414B5631`. **All controller instances MUST use the same value**;
  a mismatch causes both to think they are active.
- `DLW_LEADER_POLL_INTERVAL_SECONDS` — standby poll interval (default 5.0,
  range 0.5–60.0).

**Removed environment variables (W3c):**

- `DLW_STRICT_RECOVERY` — deleted. Recovery failures now keep the controller
  in `recovering` and retry on the next leader-loop tick (heartbeats keep
  503ing; alertable from log volume).
```

- [ ] **Step 6: Run the full test suite + all lints**

Run: `uv run pytest -q 2>&1 | tail -10`
Expected: PASS — entire suite green.

Run: `uv run python tools/lint_invariants.py`
Expected: exit 0, `OK: ...`.

- [ ] **Step 7: Commit**

```bash
git add tests/e2e/test_failover_drill.py api/openapi.yaml docs/operator/executor-runbook.md
git commit -m "test(e2e)+docs: failover drill + OpenAPI + operator runbook (W3c)"
```

- [ ] **Step 8: Push and open the PR**

The controller (not the implementer subagent) handles push + PR per the established W-cycle workflow. **Stop after Step 7 and report DONE.** The push + `gh pr create` step is run by the controller after the final whole-implementation review.

---

## Self-Review

**1. Spec coverage:**
- §1 Goal & Scope (app-level leader election; auto-promotion; cold standby; single-PG) → all 8 tasks collectively.
- §3.1 `LeaderElector` → T2.
- §3.1 `run_leader_loop` → T3.
- §3.2 state machine → encoded in T3's loop body + verified by T8's drill.
- §3.3 lifespan restructure → T7.
- §3.4 `require_not_recovering` dep + heartbeat/poll/report wiring → T6.
- §3.5 `/health/active` endpoint → T5.
- §3.6 `Settings` fields → T1.
- §3.7 `make_app_with_state` helper + ~10-12 site migration → T4 (7 fixture sites; see migration scope refinement below).
- §4 zero schema changes → no alembic task (correct).
- §5 wire format → T5 (`/health/active`), T6 (503 response), T8 Step 3 (OpenAPI).
- §6 error matrix → covered by T2 (connection drop), T3 (recovery failure stays in recovering), T8 (chaos drill).
- §7 testing strategy:
  - 7.1 `test_leader_election.py` → T2 (6 cases).
  - 7.1 `test_leader_loop.py` → T3 (4 cases).
  - 7.1 `test_failover_drill.py` → T8 (3 cases).
  - 7.1 `test_recovery_barrier.py` → T6 (3 cases).
  - 7.1 `test_health_active.py` → T5 (3 cases).
  - 7.1 executor client 503-retry → T6.
  - 7.2 migrations → T4 (covers the 7 affected fixture sites; the spec said "10-12 sites" but the actual count of fixture sites grepped was 7 — the discrepancy is because the spec counted some non-fixture test functions that inline `app = create_app()`; the 7 fixture sites are the canonical ones to migrate).
- §8 acceptance criteria → all line items map to a task.
- §9 phasing (M1/M2/M3) → matches the three milestones.

**2. Placeholder scan:** No "TBD" / "TODO" / "implement later" / "handle edge cases" / "similar to Task N". Every code step has a complete code block or a precise before/after snippet. The one judgement call left to the implementer (T8 Step 3 placing the new OpenAPI operation among siblings) is bounded — "search for `/health/live` / `/health/ready` to find the right location".

**3. Type consistency:** `ControllerState = Literal["standby", "recovering", "active"]` is defined in T2 (`leader_election.py`) and used in T3's `run_leader_loop` signature. The barrier's default `"active"` (T6) and `/health/active`'s default `"standby"` (T5) are intentional and consistent with the spec §3.4/§3.5. `LeaderElector.try_acquire / verify / release` signatures are stable across T2 (definition), T3 (use in `run_leader_loop`), T7 (use in lifespan), and T8 (use in failover drill). `make_app_with_state(ephemeral_ca, *, enrollment_token, controller_state="active")` — same signature in T4 definition and across all migration sites + the new T5/T6 tests.
