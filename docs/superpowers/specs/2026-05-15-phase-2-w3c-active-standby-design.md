# Phase 2 Week 3c — Active/Standby Controller Design

> **Status:** Draft (brainstormed 2026-05-15).
> **Companion plan:** `docs/superpowers/plans/2026-05-15-phase-2-w3c-active-standby.md` (to be written by writing-plans skill after spec approval).
> **Roadmap source:** `docs/v2.0/08-mvp-roadmap.md` §2.6 — Phase 2 Week 3 Day 5 ("Active/standby + chaos 演练"). Phase 2 exit criterion: "Active/standby switch RTO ≤ 10min (CH-Q1)".
> **Companion split (W3a / W3b):** W3a (mTLS + JWT + HMAC) merged — PR #12. W3b (HF reverse proxy) merged — PR #13. W3c is the last Phase 2 W3 sub-week.
> **Operations source:** `docs/v2.0/05-operations.md` §6.1 (Controller active/standby).
> **Invariant source:** `docs/v2.0/INVARIANTS.md` row 33 ("standby 提升后进入 recovery_in_progress；所有心跳响应 503 直到三向对账完成").
> **Closes:** OPS-04 (partial — the app-level half. PG-level HA stays in Phase 4.).

---

## 1. Goal & Scope

### 1.1 Goal

App-level controller leader election so two controller instances run active/standby with automatic failover.

**Mechanism.** The instance holding `pg_try_advisory_lock(<active_lock_id>)` (PG session-level lock) is the **active**. It runs `run_recovery_routine`, then the sweep loop, and answers `GET /health/active` with 200. The standby polls for the lock; when the active dies, PG auto-releases the lock on the dropped session and the standby acquires it, auto-promoting through a `standby → recovering → active` state machine. While `recovering`, the executor-loop endpoints (heartbeat / poll / report) return 503 (INVARIANT 33).

After W3c, single-instance deployments still work identically — the lone instance simply acquires the lock instantly. Operators can run a second instance whenever they're ready.

### 1.2 In scope

| Item | Where |
|---|---|
| `LeaderElector` — dedicated NullPool engine holding `pg_try_advisory_lock` | `src/dlw/services/leader_election.py` (new) |
| `run_leader_loop` — async coroutine driving the state machine | same file |
| `main.py` lifespan restructure — recovery + sweep become leader-gated | `src/dlw/main.py` |
| `GET /health/active` — LB target | `src/dlw/api/health.py` |
| `require_not_recovering` dependency — 503 barrier on executor-loop endpoints (heartbeat/poll/report) | `src/dlw/api/_recovery_barrier.py` (new), wired into `api/executors.py` + `api/subtasks.py` |
| Two new `Settings` fields: `active_lock_id`, `leader_poll_interval_seconds` | `src/dlw/config.py` |
| Chaos-drill integration test | `tests/e2e/test_failover_drill.py` (new) |
| Operator runbook note | `docs/operator/executor-runbook.md` |

### 1.3 Non-goals (deferred — explicit list)

| Item | Where |
|---|---|
| PG streaming replication / PG primary failover (`promote-standby.sh`) | **Phase 4** — CH-Q3 ("PG primary 故障"). W3c runs against a single shared PostgreSQL. |
| chaos-mesh automation | **Phase 4** — Phase 4 §4.2 explicitly. |
| Warm / read-only standby | rejected in §1; cold standby only (LB routes off `/health/active`). |
| Active-active multi-leader scheduling | explicitly rejected in `05 §6.1`. v2.0 single active only. |
| LB / k8s Helm wiring | the deploy-side health-check config is a deploy task (not part of W3c). W3c only provides the `/health/active` endpoint. |
| Manual demote / step-down admin endpoint | not needed; auto-promotion only. Restart = step down. |
| `DLW_STRICT_RECOVERY` env knob | deleted as part of the lifespan restructure (the leader loop's retry-on-promote-failure replaces it). |
| Active-side metrics (`dlw_controller_state_*` gauges, `time_to_promote` histogram) | useful for operators but a Phase-3 observability polish — out of W3c. The state is observable via `/health/active` + logs. |

---

## 2. Tech Stack Additions

**None.** `pg_try_advisory_lock` / `pg_advisory_unlock` are built into PostgreSQL; SQLAlchemy async + asyncpg already in use; FastAPI dependency pattern already in use. No new runtime deps, no new dev deps, no new CI jobs, **zero alembic migrations**.

---

## 3. Components

### 3.1 New: `src/dlw/services/leader_election.py`

Owns both `LeaderElector` (the lock primitive) and `run_leader_loop` (the state-machine coroutine). Pure async logic — no HTTP, no FastAPI imports — so it's testable in isolation against the local PG.

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
            except Exception:
                pass
            self._conn = None


async def run_leader_loop(
    *,
    elector: LeaderElector,
    poll_interval_seconds: float,
    set_state: Callable[[ControllerState], None],
    on_promote: Callable[[], Awaitable[None]],   # runs recovery_routine
    on_active: Callable[[], Awaitable[None]],    # starts the sweep task
    on_step_down: Callable[[], Awaitable[None]], # cancels the sweep task
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
                    state = "recovering"; set_state(state)
                    logger.info("leader: acquired lock, running recovery")
                    try:
                        await on_promote()
                    except Exception:
                        logger.exception("leader: recovery failed; will retry next tick")
                        # Stay in `recovering` — heartbeats keep 503ing.
                        # Don't release the lock (another instance can't fix it).
                        await _sleep_or_shutdown(shutdown, poll_interval_seconds)
                        continue
                    state = "active"; set_state(state)
                    await on_active()
                    logger.info("leader: promoted to active")
            elif state in ("recovering", "active"):
                if not await elector.verify():
                    logger.warning("leader: lost lock, stepping down to standby")
                    await on_step_down()
                    state = "standby"; set_state(state)
                    continue  # retry acquire immediately
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

### 3.2 State machine

```
   ┌──── start ────┐
   ▼               │
[standby] ── acquire lock ──▶ [recovering] ── recovery_routine OK ──▶ [active]
   ▲                                                                    │
   │                                                                    │
   └────────────── connection dropped / process restart ────────────────┘
```

- `standby` — leader loop polls `try_acquire` every `leader_poll_interval_seconds` (default 5.0). `/health/active` returns 503. Sweep loop not running. Executor-loop endpoints pass through the recovery barrier (no 503), but the LB routes nothing here anyway.
- `recovering` — lock acquired; `run_recovery_routine` in progress. `/health/active` returns 200 (LB cuts over). Heartbeat / poll / report return 503 `{code: "CONTROLLER_RECOVERING"}` — INVARIANT 33. Stays here on `on_promote` failure (retried next tick); does **not** release the lock (another instance can't fix it).
- `active` — recovery done; sweep task spawned by the leader loop. Normal operation. The loop calls `elector.verify()` each tick; if it returns False (connection lost), the loop cancels the sweep, steps down to `standby`, and tries to re-acquire.

### 3.3 Modified: `src/dlw/main.py` lifespan

The lifespan loses its unconditional `run_recovery_routine` + `_sweep_loop_main` spawn and gains leader-loop wiring. The W3a auth bootstrap (CA / JWT keypair / nonce store / enrollment token) stays unconditional — both roles need it ready so promotion is instant.

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from dlw.db.session import get_engine, reset_engine
    from dlw.services.recovery import run_recovery_routine
    from dlw.services.leader_election import LeaderElector, run_leader_loop

    factory = async_sessionmaker(get_engine(), expire_on_commit=False)

    # W3a auth bootstrap — UNCHANGED. Both active and standby need this ready.
    install_transport_scope_patch()
    # ... bootstrap_ca / ensure_server_cert / bootstrap_keypair / NonceStore /
    #     enrollment_token / app.state.{ca,jwt_keypair,nonce_store,enrollment_token}
    settings = get_settings()

    # W3c: controller state + leader loop.
    app.state.controller_state = "standby"
    shutdown = asyncio.Event()
    elector = LeaderElector(db_url=settings.db_url, lock_id=settings.active_lock_id)
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
        poll_interval_seconds=settings.leader_poll_interval_seconds,
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

`DLW_STRICT_RECOVERY` is **deleted** — the leader loop's "stay in recovering, retry next tick" replaces it.

### 3.4 New: `src/dlw/api/_recovery_barrier.py`

```python
"""FastAPI dep that 503s executor-loop calls while the controller is recovering
(INVARIANT 33). Attached to heartbeat / poll / report endpoints."""
from __future__ import annotations

from fastapi import HTTPException, Request


async def require_not_recovering(request: Request) -> None:
    state = getattr(request.app.state, "controller_state", "active")
    if state == "recovering":
        raise HTTPException(
            status_code=503,
            detail={"code": "CONTROLLER_RECOVERING",
                    "message": "controller recovering after failover, retry shortly"},
        )
```

Wired into the three executor-loop routes via `dependencies=[Depends(require_not_recovering)]`:
- `POST /api/v1/executors/{executor_id}/heartbeat` (`api/executors.py`)
- `POST /api/v1/executors/{executor_id}/poll` (`api/executors.py`)
- `POST /api/v1/subtasks/{subtask_id}/report` (`api/subtasks.py`)

The HF reverse-proxy endpoint (`/api/v1/hf-proxy/subtask/{id}`) is **not** barriered — it's a streaming passthrough that doesn't mutate state, and an executor that has work already in flight should be able to finish downloading even during recovery (the bytes flow controller-side → HF → executor; nothing the recovery routine touches).

### 3.5 Modified: `src/dlw/api/health.py`

Add one endpoint:

```python
@router.get("/active")
async def active(request: Request) -> dict[str, str]:
    """LB target — 200 iff this instance holds the leader lock."""
    state = getattr(request.app.state, "controller_state", "standby")
    if state in ("recovering", "active"):
        return {"status": "active", "controller_state": state}
    raise HTTPException(status_code=503, detail={"controller_state": state})
```

`/health/live` and `/health/ready` are **unchanged** — they remain the k8s livenessProbe and readinessProbe respectively. `/health/active` is for LB-level routing.

### 3.6 Modified: `src/dlw/config.py`

`Settings` gains two fields:

```python
    # Phase 2 W3c — controller leader election
    active_lock_id: int = Field(default=0x444C5743_414B5631, ge=1)  # 'DLWC AKV1'
    leader_poll_interval_seconds: float = Field(default=5.0, ge=0.5, le=60.0)
```

The `active_lock_id` default is an arbitrary fixed bigint. Both controller instances MUST use the same value to coordinate on the same lock — that's why it's a fixed default, not randomized.

### 3.7 Test infrastructure: `make_app_with_state` conftest helper

The lifespan restructure means every API/e2e test that mounts `create_app()` via `ASGITransport` now needs `app.state.controller_state = "active"` (otherwise the new 503 barrier on heartbeat/poll/report fires). Currently each test inlines its app setup (~10-12 sites). W3c adds a small helper in `tests/conftest.py`:

```python
def make_app_with_state(ephemeral_ca, *, enrollment_token: str,
                       controller_state: str = "active"):
    """Build a controller app for ASGI-transport tests with app.state pre-seeded
    (skips the lifespan bootstrap). Defaults controller_state to 'active' so the
    W3c recovery barrier doesn't fire."""
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

Existing tests that inline `app = create_app()` + `app.state.* = ...` migrate to one call to this helper. Tests that need a non-active state (the new recovery-barrier tests, the `/health/active` tests) pass `controller_state="standby"` or `"recovering"` explicitly.

---

## 4. Schema Changes

**None.** No new table, no new column, no alembic migration. Advisory locks need no schema. W3c is the second consecutive sub-week with no alembic migration (W3b was the first).

---

## 5. Wire Format Changes

### 5.1 New endpoint `GET /health/active`

| Aspect | Value |
|---|---|
| Auth | none (it's a health check; the LB calls it) |
| Response 200 | `{"status": "active", "controller_state": "recovering" | "active"}` |
| Response 503 | `{"detail": {"controller_state": "standby"}}` |

### 5.2 New 503 response on three executor-loop endpoints

| Endpoint | New 503 response |
|---|---|
| `POST /api/v1/executors/{executor_id}/heartbeat` | `{"detail": {"code": "CONTROLLER_RECOVERING", "message": ...}}` |
| `POST /api/v1/executors/{executor_id}/poll` | same |
| `POST /api/v1/subtasks/{subtask_id}/report` | same |

Executors handle this through the existing tenacity `_retry` in `src/dlw/executor/client.py` (3 attempts, exponential backoff to ~4s max) — no new client-side code required. The retry exhausts in seconds; by then recovery has typically completed and the next attempt succeeds. A new test in `tests/executor/test_client.py` asserts this.

### 5.3 Config surface

- `Settings` gains: `active_lock_id` (env `DLW_ACTIVE_LOCK_ID`, default `0x444C5743414B5631`), `leader_poll_interval_seconds` (env `DLW_LEADER_POLL_INTERVAL_SECONDS`, default 5.0).
- `Settings` loses: nothing.
- Env `DLW_STRICT_RECOVERY` is **removed** from the codebase. Documented in the operator runbook.

### 5.4 OpenAPI

`api/openapi.yaml` gains:
- `/health/active` GET operation (200 / 503).
- A `503 CONTROLLER_RECOVERING` response variant added to the three executor-loop operations (heartbeat / poll / report). The detail-body schema (`{code, message}`) is added as a component or inlined.

---

## 6. Error Handling Matrix

| Situation | Behaviour |
|---|---|
| First instance starts, PG up | acquires lock → recovers → active |
| Two instances start simultaneously | PG guarantees exactly one wins; the other stays standby polling |
| Active dies (kill / crash / pod evicted) | TCP-close detected (tcp_keepalive ≤ 30s) → PG releases lock → standby's next poll-interval acquires → standby promotes |
| Active's PG connection has a half-open TCP (network partition, active alive but invisible) | tcp_keepalive_idle=30 + tcp_keepalives_interval=10 → connection RST detected → lock released; floor for this failure mode is ~30-90s, well under the 10min RTO |
| `run_recovery_routine` raises on promote | leader loop logs, stays in `recovering`, retries next tick; heartbeats keep 503ing; operator-visible via logs and metrics (alertable from `/health/active`=200 + heartbeat 503 imbalance) |
| Active's lock-holding connection dies while it's `active` | `elector.verify()` returns False → cancel sweep → state → `standby` → next tick re-acquires (or another instance does) |
| Graceful shutdown (SIGTERM) of active | lifespan finally: cancel sweep; cancel leader task; `elector.release()` (explicit `pg_advisory_unlock` + close conn); standby acquires within poll-interval |
| Both instances briefly think they have the lock (split-brain) | Cannot happen — `pg_try_advisory_lock` is atomic and session-scoped; the lock is held by **exactly one** PG session at a time |
| `active_lock_id` mismatch between two instances | both think they're active; double-write into the same PG. Mitigation: fixed default in `Settings`, documented as "must match across all controller instances" in the operator runbook |
| Operator runs old image (pre-W3c) alongside new image | Old image doesn't try to acquire the lock and runs its full lifespan (recovery + sweep). New image acquires lock + runs leader loop. Both run their sweeps → harmless duplicate sweeps against `SKIP LOCKED` queries (correctness preserved by §4.2's SKIP LOCKED). Worth flagging in rollout docs but not a correctness bug. |

---

## 7. Testing Strategy

### 7.1 New unit/integration tests (~15 new cases)

| # | File | Case | Asserts |
|---|---|---|---|
| 1-6 | `tests/services/test_leader_election.py` | `LeaderElector` primitives | first acquire / second blocked / release frees / connection drop frees / verify True when held / verify False after drop |
| 7-10 | `tests/services/test_leader_loop.py` | `run_leader_loop` with fake callbacks | standby polls until lock free → promotes / promote failure retries / step-down on connection loss / shutdown exits cleanly |
| 11-13 | `tests/e2e/test_failover_drill.py` | The chaos drill | two instances → exactly one active / kill active → standby promotes within ≤ 3 × poll_interval through `recovering` to `active` / promoted standby actually ran `run_recovery_routine` (verified via a pre-seeded stale `multipart_upload_id` row that gets reconciled) |
| 14-16 | `tests/api/test_recovery_barrier.py` | Barrier dep | heartbeat 503 when recovering; same for poll + report / endpoints normal when active / detail body shape (`code == "CONTROLLER_RECOVERING"`) |
| 17-19 | `tests/api/test_health.py` (extend) | `/health/active` | 503 when standby / 200 when recovering / 200 when active |
| 20 | `tests/executor/test_client.py` (extend) | Client tenacity retry survives a 503 | first call returns 503 `CONTROLLER_RECOVERING`, second returns 200 → tenacity retry succeeds (no new client code needed; this is a behavioral assertion) |

(Numbering nominal — final count ~15-20 depending on how cases are split.)

### 7.2 Migration of existing tests

The lifespan restructure means every API/e2e test that uses `ASGITransport` (and thus bypasses lifespan) must set `app.state.controller_state = "active"` so the new 503 barrier on heartbeat/poll/report doesn't fire.

Affected files (one line per site, or one call to `make_app_with_state`):
- `tests/e2e/test_happy_path.py`
- `tests/e2e/test_executor_e2e.py`
- `tests/e2e/test_executor_auth_e2e.py`
- `tests/api/test_hf_proxy.py`
- `tests/api/test_executors.py`
- `tests/api/test_subtasks.py`
- `tests/api/test_register_endpoint.py`
- `tests/api/test_renew_endpoint.py`
- `tests/api/test_cancel_endpoint.py`

~10-12 sites total. The `make_app_with_state` helper (§3.7) is added as part of W3c; migrating each site is a one-line change.

### 7.3 Test approach for the chaos drill

`tests/e2e/test_failover_drill.py` is the W3c centrepiece. It runs **two real `LeaderElector` instances + two `run_leader_loop` tasks** against the same local test PG (no app/HTTP/k8s involved — pure leader-loop simulation). One acquires the lock; the test cancels its loop task and closes its elector connection abruptly; within ≤ 3 × `leader_poll_interval` the other instance is observed transitioning `standby → recovering → active`. Pre-seeded stale rows verify that `run_recovery_routine` actually ran on promotion.

This is the practical proof-of-concept for "Active/standby switch RTO ≤ 10min" — `leader_poll_interval=0.5` keeps the test fast (sub-2-second failover); production tunes to 5s.

The manual quarterly CH-Q1 GameDay (`07 §6.2`) is documented but executed by SRE later — out of W3c's test deliverable.

### 7.4 Not tested

- Real two-process failover (W3c tests use two electors in one process — a full process-kill scenario needs CH-Q1).
- Real PG primary failover (CH-Q3 — Phase 4).
- chaos-mesh automation (Phase 4).
- LB cutover latency (deploy-side, not a test concern).

### 7.5 CI 12-check expectations

| Check | W3c impact |
|---|---|
| pytest | +~15 new + ~10-12 migrated fixture sites |
| OpenAPI lint | `/health/active` route documented; 503 `CONTROLLER_RECOVERING` documented on three executor-loop routes |
| Invariant + cross-ref lint | INVARIANT 33 referenced in code comments + spec/plan cross-refs |
| Markdown lint | spec/plan cross-ref `05 §6.1` + `INVARIANTS §33` |
| Other 8 | no change |

---

## 8. Acceptance Criteria

- [ ] `LeaderElector` with `try_acquire` / `verify` / `release` on a dedicated NullPool engine (tcp_keepalive tuned).
- [ ] `run_leader_loop` driving `standby → recovering → active` with promote / active / step-down callbacks.
- [ ] `main.py` lifespan: recovery + sweep are leader-gated; `DLW_STRICT_RECOVERY` env knob deleted.
- [ ] `GET /health/active` returns 200 iff the instance is `recovering` or `active`.
- [ ] `require_not_recovering` dependency on heartbeat / poll / report → 503 `CONTROLLER_RECOVERING` while `recovering`.
- [ ] Two new `Settings` fields: `active_lock_id`, `leader_poll_interval_seconds`.
- [ ] ~15 new pytest cases pass; ~10-12 migrated app-fixture sites pass; `make_app_with_state` conftest helper exists.
- [ ] `tests/e2e/test_failover_drill.py` proves the standby-promotes-when-active-dies path end-to-end (two-elector simulation).
- [ ] Full suite green; OpenAPI lint clean (`/health/active` + 503 `CONTROLLER_RECOVERING`); existing invariant lint passes.
- [ ] Operator runbook notes the app-level lock and its relationship to the PG-level `promote-standby.sh`.
- [ ] **Zero alembic migrations**; no new runtime deps; no new CI jobs.

---

## 9. Implementation Phasing (preview for plan)

3 milestones, ~7-8 tasks.

- **M1 — Lock primitive.** `LeaderElector` + `test_leader_election.py` (~6 cases) + the two new `Settings` fields + their config test. Pure unit work against local test PG; no app integration yet.
- **M2 — Leader loop + lifespan restructure + barriers.** `run_leader_loop` + `test_leader_loop.py` (~4 cases, fake callbacks); `main.py` lifespan restructure; `/health/active` endpoint + tests; `require_not_recovering` dep + wiring + tests; `make_app_with_state` conftest helper + ~10-12 existing test fixtures migrated; the executor `test_client.py` 503-retry case.
- **M3 — Chaos drill + docs + PR.** `test_failover_drill.py` (the centrepiece); OpenAPI updates (`/health/active`, `CONTROLLER_RECOVERING` 503); operator runbook section; full suite + lint; commit; PR.

Branch: `feat/phase-2-w3c-active-standby` (already created off `main` after PR #13 merge).

---

## 10. References

- Spec source: brainstormed 2026-05-15 (this document).
- Roadmap: `docs/v2.0/08-mvp-roadmap.md` §2.6 (Phase 2 W3 Day 5) + §2.5 exit criterion + §2.7 risk note.
- Operations: `docs/v2.0/05-operations.md` §6.1 (Controller active/standby), §6.4 (RB-01).
- Invariant 33: `docs/v2.0/INVARIANTS.md` row 33 ("standby 提升后进入 recovery_in_progress；所有心跳响应 503 直到三向对账完成").
- Predecessor specs:
  - W3a: `docs/superpowers/specs/2026-05-14-phase-2-w3a-mtls-jwt-hmac-design.md` (the auth chain whose endpoints the recovery barrier wraps)
  - W3b: `docs/superpowers/specs/2026-05-14-phase-2-w3b-hf-reverse-proxy-design.md` (HF proxy — explicitly NOT barriered; in-flight downloads should complete during recovery)
  - W1: `docs/superpowers/specs/2026-05-11-phase-2-week-1-fence-token-recovery-design.md` (`run_recovery_routine` — the work the leader loop calls on promote)
- W3b PR (merged): https://github.com/l17728/modelpull/pull/13 (squash `2924b6e`).
