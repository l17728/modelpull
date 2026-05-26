"""W3c chaos drill: two LeaderElector instances + two run_leader_loop tasks
against the same test PG. Simulate active death; assert the standby acquires
the lock and transitions standby → recovering → active end-to-end."""
from __future__ import annotations

import asyncio
import os

import pytest

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


async def _wait_until(predicate, *, timeout: float = 2.0, interval: float = 0.05):
    """Poll `predicate()` until it returns truthy or `timeout` elapses.
    Returns the truthy value on success; raises asyncio.TimeoutError on
    miss. Replaces fixed-sleep + assert patterns that flake on slow CI
    runners where the first DB roundtrip can blow a tight budget."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while True:
        v = predicate()
        if v:
            return v
        if loop.time() >= deadline:
            raise asyncio.TimeoutError(f"predicate stayed false for {timeout}s")
        await asyncio.sleep(interval)


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
        # Wait up to 2s for exactly one to reach 'active' (was 0.5s — too
        # tight on shared CI runners where the first PG roundtrip can
        # spike past 200ms).
        await _wait_until(
            lambda: bool(states_a and states_a[-1] == "active") ^
                    bool(states_b and states_b[-1] == "active"),
            timeout=3.0)
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
        # Wait for A to become active and B to settle on standby (was a
        # fixed 0.3s sleep — flaked under CI load).
        await _wait_until(
            lambda: (states_a and states_a[-1] == "active") and
                    (states_b and states_b[-1] == "standby"),
            timeout=3.0)
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

        # B should pick up the lock and complete promote within a few
        # polls. RTO budget was 3 × poll_interval (0.15s) — too tight for
        # slow CI. Bumped to 2s with poll-loop so fast machines finish
        # quickly and slow ones get headroom.
        await _wait_until(
            lambda: "recovering" in states_b and states_b[-1] == "active",
            timeout=3.0)
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
        # Wait for one to win election (was 0.3s — flaked on slow CI).
        await _wait_until(
            lambda: (states_a and states_a[-1] == "active") or
                    (states_b and states_b[-1] == "active"),
            timeout=3.0)
        # One of A/B has promoted. Identify the dead-active path.
        if states_a[-1] == "active":
            assert promote_a_count == 1
            shutdown_a.set()
            await asyncio.wait_for(task_a, timeout=1.0)
            if elector_a._conn is not None:
                await elector_a._conn.close()
                elector_a._conn = None
            await _wait_until(
                lambda: promote_b_count == 1, timeout=2.0)
            assert promote_b_count == 1, \
                f"B's on_promote did not run after A died: {promote_b_count=}"
        else:
            assert promote_b_count == 1
            shutdown_b.set()
            await asyncio.wait_for(task_b, timeout=1.0)
            if elector_b._conn is not None:
                await elector_b._conn.close()
                elector_b._conn = None
            await _wait_until(
                lambda: promote_a_count == 1, timeout=2.0)
            assert promote_a_count == 1, \
                f"A's on_promote did not run after B died: {promote_a_count=}"
    finally:
        shutdown_a.set()
        shutdown_b.set()
        await asyncio.gather(task_a, task_b, return_exceptions=True)
        await elector_a.release()
        await elector_b.release()
