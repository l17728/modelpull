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
