# Phase 2 Week 1 — Fence Token + Recovery Design

> Brings the distributed-correctness fence (executor epoch + assignment token)
> and the startup/runtime crash-recovery surface to Phase 1's single-executor
> happy path. Closes the gap between Phase 1 §1.5 acceptance and the
> distributed-correctness invariants 6/7/9/33/46 of `docs/v2.0/03`.

- **Status**: design approved (2026-05-11)
- **Phase**: Phase 2, Week 1 (first of three Phase 2 weeks — fence/recovery,
  multi-executor + state machine, mTLS + Active/Standby)
- **Source roadmap**: `docs/v2.0/08-mvp-roadmap.md` §2.6 Week 1 task breakdown
- **Companion**: `docs/v2.0/03-distributed-correctness.md` §2 (Fence Token + Epoch),
  §3 (Recovery routine + three-way verification), §5 (Reclaim semantics);
  `docs/v2.0/02-protocol.md` §6 (CAS-then-enqueue wire format).
- **Author**: l17728
- **Reviewer**: TBD (2-agent multi-perspective review before plan execution)

---

## 1. Goal & Non-Goals

### 1.1 Goal

Add a single coherent fence layer to the executor protocol — executor epoch +
assignment token — and the startup-once + periodic recovery surface that
detects, fences, and reclaims work from a crashed or stale executor without
losing data and without double-committing.

End of plan: a crashed executor's claimed-but-unfinished subtasks return to
`pending` after a bounded window, with an epoch fence preventing the zombie
executor from overwriting work that a peer (or its own re-joined self) has
picked up. Three-way verification (head + size) handles the case where the
crash happened mid-multipart-upload.

### 1.2 Non-goals (deferred — explicit list)

| Item | Deferred to | Reason |
|------|-------------|--------|
| Full task-status state machine (downloading / uploading / verifying_remote / cancelling / paused_*) | P2-W2 | Phase 1 used `assigned → succeeded` direct transition; adding intermediate states requires protocol changes + many tests. |
| `executor_jwt` + cert-fingerprint binding | P2-W3 | Belongs to the mTLS subsystem; epoch fence is orthogonal and ships first. |
| `ChecksumSHA256` server-side verification on multipart parts | P2-W2 | Requires changing W4's `upload_part` to pass `ChecksumAlgorithm='SHA256'` + moto compatibility check; not needed for head+size three-way. |
| Multi-executor scheduler fairness / priority queue | P2-W2 | Single executor `FOR UPDATE SKIP LOCKED` is fine here. |
| HMAC heartbeat (nonce + timestamp) | P2-W3 | Replay-attack defense; lives with mTLS. |
| Node state-machine `degraded ↔ suspect` + `probationary` | P2-W2 | Full lifecycle states; this plan only adds the `unhealthy` transition for reclaim. |
| `verifying` task recovery branch | P2-W2 | Phase 1 has no `verifying` task state. |
| HF global 429 throttle state recovery | Phase 3 | Phase 1 is single source. |
| Bucket lifecycle config (24h auto-abort multiparts) | Phase 3 / ops | Helm chart concern, not code. |
| `chunks_completed` / `chunks_total` resume | P2-W2 | Lives with chunk-level multi-threaded download. |

---

## 2. Tech Stack Additions

No new libraries. Existing stack covers everything:

- SQLAlchemy 2.x `update()` with `WHERE` fence + `returning()` for atomic row-count.
- PostgreSQL `INSERT ... ON CONFLICT (id) DO UPDATE SET epoch = epoch + 1 RETURNING epoch` for the atomic epoch bump.
- FastAPI `Depends` + `Header` + `Path` parameters compose the new `require_executor_epoch` guard.
- asyncio.create_task in lifespan for the periodic reclaim loop.
- `moto[s3]` (already W4 dep) covers head_object + abort_multipart_upload in tests.

---

## 3. Components

### 3.1 New: `dlw.auth.executor_epoch`

```python
# src/dlw/auth/executor_epoch.py — NEW
async def require_executor_epoch(
    executor_id: str = Path(...),
    x_executor_epoch: int = Header(..., alias="X-Executor-Epoch"),
    session: AsyncSession = Depends(_session),
) -> Executor:
    """Verifies X-Executor-Epoch header matches executor.epoch in DB.

    Returns the Executor row for handlers to use (saves a re-fetch).
    Composes with require_bearer (both run via Depends; Bearer first per route order).
    """
    ex = await session.get(Executor, executor_id)
    if ex is None:
        raise HTTPException(status_code=404, detail="executor not found")
    if ex.epoch != x_executor_epoch:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "EPOCH_MISMATCH",
                "expected": ex.epoch,
                "got": x_executor_epoch,
            },
        )
    return ex
```

### 3.2 New: `dlw.services.recovery`

```python
# src/dlw/services/recovery.py — NEW
from dataclasses import dataclass

@dataclass
class RecoveryStats:
    three_way_checked: int = 0
    verified_recovered: int = 0
    reset_to_pending: int = 0
    size_mismatch_purged: int = 0
    no_multipart_reset: int = 0
    orphan_aborted: int = 0


async def run_recovery_routine(session: AsyncSession) -> RecoveryStats:
    """One-shot startup recovery. See §3.3 for full pseudocode."""

async def verify_remote_state(
    session: AsyncSession, sub: FileSubTask
) -> Literal["verified", "missing", "size_mismatch"]:
    """Phase 1 three-way: head + size only. SHA256 deferred to P2-W2."""

async def reclaim_stale_executors(session: AsyncSession) -> int:
    """Scans executors.last_heartbeat_at; marks stale + reclaims their subtasks."""
```

Helper `_abort_multipart_silently(s3, bucket, key, upload_id)` swallows
`ClientError`s and logs them; S3 lifecycle (Phase 3 ops) is the safety net.

### 3.3 Modified: `dlw.services.scheduler`

```python
# claim_one_subtask — add executor_epoch + assigned_at writes
async def claim_one_subtask(
    session: AsyncSession,
    executor_id: str,
    executor_epoch: int,                     # NEW
) -> tuple[FileSubTask | None, uuid.UUID | None]:
    """Atomic claim. P2-W1 additions: writes executor_epoch + assigned_at."""
    ...
    sub.status = "assigned"
    sub.executor_id = executor_id
    sub.executor_epoch = executor_epoch       # NEW
    sub.assignment_token = token
    sub.assigned_at = datetime.now(UTC)       # NEW
    return sub, token


# complete_subtask — add executor_epoch verify gate
async def complete_subtask(
    session, subtask_id, *,
    final_status, actual_sha256, bytes_downloaded, error,
    assignment_token=None,
    executor_epoch=None,                      # NEW kwarg
    s3_key=None,
) -> tuple[FileSubTask, DownloadTask]:
    ...
    if (
        assignment_token is not None
        and sub.assignment_token != assignment_token
    ):
        raise ValueError(...)                 # W2-F unchanged
    if executor_epoch is not None and sub.executor_epoch != executor_epoch:
        raise ValueError(                     # NEW gate
            f"subtask {subtask_id} executor_epoch mismatch "
            f"(expected={sub.executor_epoch}, got={executor_epoch})"
        )
    # ... rest unchanged


# NEW: reclaim_subtasks — fenced by (executor_id, epoch)
async def reclaim_subtasks(
    session: AsyncSession, executor_id: str, current_epoch: int
) -> int:
    """Single UPDATE; rowcount tells how many subtasks fell into reclaim."""
```

### 3.4 Modified: `dlw.services.executor_service.join_executor`

```python
async def join_executor(session: AsyncSession, body: ExecutorJoin) -> Executor:
    """Atomic upsert: INSERT or epoch += 1 on conflict.

    Phase 1 W2 implementation re-fetched after INSERT/UPDATE — replaced by
    PostgreSQL ON CONFLICT to make epoch bump atomic against concurrent join.
    """
    stmt = pg_insert(Executor).values(
        id=body.id,
        host_id=body.host_id,
        cert_fingerprint=body.cert_fingerprint,
        status="joining",                     # matches Phase 1 W2 semantics
        epoch=1,                              # NEW (first-time)
        capabilities=body.capabilities,
    ).on_conflict_do_update(
        index_elements=["id"],
        set_=dict(
            # Re-join resets to 'joining' even if previously 'unhealthy'
            # (reclaim_stale_executors sets 'unhealthy'); first heartbeat
            # post-rejoin flips back to 'healthy'.
            status="joining",
            host_id=body.host_id,
            cert_fingerprint=body.cert_fingerprint,
            epoch=Executor.__table__.c.epoch + 1,   # atomic bump
        ),
    ).returning(Executor)
    row = (await session.execute(stmt)).scalar_one()
    return row
```

### 3.5 Modified: `dlw.api.{executors,subtasks}`

Each endpoint adds `Depends(require_executor_epoch)` and reads the returned
Executor row instead of re-fetching. `/report` forwards `executor_epoch` into
`complete_subtask` for the new fence verification.

```python
# api/executors.py
@router.post("/{executor_id}/heartbeat", dependencies=[Depends(require_bearer)])
async def post_heartbeat(
    body: ExecutorHeartbeat,
    executor: Executor = Depends(require_executor_epoch),
    session: AsyncSession = Depends(_session),
) -> ExecutorRead:
    await record_heartbeat(session, executor.id, body)
    await session.commit()
    return ExecutorRead.model_validate(executor)


@router.post("/{executor_id}/poll", dependencies=[Depends(require_bearer)])
async def post_poll(
    executor: Executor = Depends(require_executor_epoch),
    session: AsyncSession = Depends(_session),
) -> AssignmentResponse:
    sub, token = await claim_one_subtask(session, executor.id, executor.epoch)
    # ... rest unchanged
```

### 3.6 Modified: `dlw.executor.client.ControllerClient`

```python
class ControllerClient:
    def __init__(self, base_url, bearer_token, ...):
        ...
        self._epoch: int | None = None              # learned from /join

    async def join(self, ...) -> dict:
        resp = await self._post("/api/v1/executors/join", ...)
        self._epoch = resp["epoch"]                 # NEW
        return resp

    def _epoch_headers(self) -> dict[str, str]:
        if self._epoch is None:
            return {}
        return {"X-Executor-Epoch": str(self._epoch)}

    async def heartbeat/poll/report(self, ...):
        # PATCH: use self._client.post(..., headers={...self._epoch_headers()})
        ...

    async def _post(self, path, json_body=None, extra_headers=None):
        # Each call: merge bearer header + (optional) X-Executor-Epoch
        ...
```

### 3.7 Modified: `dlw.executor.runner.ExecutorRunner`

```python
class ExecutorRunner:
    async def _poll_and_execute_loop(self):
        while not self._shutdown.is_set():
            try:
                resp = await self._client.poll(...)
                if resp.get("assigned"):
                    await self._execute_subtask(...)
                    continue
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 401:
                    detail = e.response.json().get("detail", {})
                    if isinstance(detail, dict) and detail.get("code") == "EPOCH_MISMATCH":
                        logger.warning(
                            "EPOCH_MISMATCH (expected=%s got=%s); re-joining",
                            detail.get("expected"), detail.get("got"),
                        )
                        await self._rejoin()      # NEW
                        continue
                logger.warning("poll failed: %s", e)
            except Exception as e:
                logger.warning("poll failed: %s", e)
            # ... pacing wait unchanged

    async def _rejoin(self):
        """Discards any in-flight assignment + fetches new epoch from /join."""
        await self._client.join(...)              # ControllerClient now updates self._epoch
```

### 3.8 Modified: `dlw.main` (lifespan)

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Run recovery_routine BEFORE serving traffic
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with factory() as session:
        stats = await run_recovery_routine(session)
        logger.info("startup recovery: %s", stats)

    # Background reclaim loop
    reclaim_task = asyncio.create_task(_reclaim_loop_main())
    try:
        yield
    finally:
        reclaim_task.cancel()
        try:
            await asyncio.wait_for(reclaim_task, timeout=2)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        await reset_engine()


async def _reclaim_loop_main():
    """Background task: every 30s, scan stale executors + reclaim."""
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    while True:
        try:
            await asyncio.sleep(30)
            async with factory() as session:
                await reclaim_stale_executors(session)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("reclaim_loop iteration failed")
```

---

## 4. Schema Changes (single alembic migration)

```sql
ALTER TABLE executors
  ADD COLUMN epoch BIGINT NOT NULL DEFAULT 0;

ALTER TABLE file_subtasks
  ADD COLUMN multipart_started_at TIMESTAMPTZ NULL;

ALTER TABLE file_subtasks
  ADD COLUMN assigned_at TIMESTAMPTZ NULL;

ALTER TABLE file_subtasks
  ADD COLUMN last_heartbeat_seen_at TIMESTAMPTZ NULL;
```

(`executor_epoch` column on `file_subtasks` already exists from W2 schema —
phase-1 placeholder, P2-W1 starts populating it.)

`multipart_upload_id` column already on `file_subtasks` (W4 schema).

Round-trip check required (W5-G discipline): `upgrade head → downgrade -1 → upgrade head` all clean.

---

## 5. Wire Format Changes

### 5.1 Executor → Controller request headers

| Endpoint | New required header |
|----------|----------------------|
| `POST /api/v1/executors/{id}/heartbeat` | `X-Executor-Epoch: <int>` |
| `POST /api/v1/executors/{id}/poll` | `X-Executor-Epoch: <int>` |
| `POST /api/v1/subtasks/{id}/report` | `X-Executor-Epoch: <int>` |

`POST /api/v1/executors/join` does NOT require the header (first contact;
controller assigns epoch).

### 5.2 New 401 response shape

```json
{
  "detail": {
    "code": "EPOCH_MISMATCH",
    "expected": 3,
    "got": 2
  }
}
```

Executor client recognises `detail.code == "EPOCH_MISMATCH"` → `_rejoin()`.

### 5.3 `ExecutorRead` response (extends, backwards-compatible)

```python
class ExecutorRead(BaseModel):
    id: str
    status: str
    health_score: int
    epoch: int                  # NEW (always present after this PR)
```

Older Phase 1 frontends ignore the new field (Pydantic forward-compat); plan
should NOT silently delete the field on serialization.

### 5.4 OpenAPI sync

`api/openapi.yaml` must be updated in the same PR (Phase 1 W4 left this drift;
P2-W1 closes it).

- Three endpoints: add header parameter `X-Executor-Epoch`.
- `AssignmentResponse`: add the missing W4 fields (`repo_id`, `revision`, `storage_config`) — backfill.
- `SubTaskRead` / `SubTaskReport`: add `s3_key` field — backfill.
- `ExecutorRead`: add `epoch`.
- New error responses: 401 EPOCH_MISMATCH schema, 409 STALE_ASSIGNMENT schema.

CI gate: `spectral lint` (existing) + `swagger-cli validate` (existing).

---

## 6. Recovery Routine Pseudocode (Phase 1 simplification)

```python
async def run_recovery_routine(session) -> RecoveryStats:
    """One-shot startup recovery. Must complete before serving traffic."""
    stats = RecoveryStats()
    threshold = datetime.now(UTC) - timedelta(seconds=120)  # 2× heartbeat default

    # Step 1: three-way (head + size) for status='assigned' with multipart_upload_id set
    in_flight = (await session.execute(
        select(FileSubTask)
          .where(FileSubTask.status == "assigned")
          .where(FileSubTask.multipart_upload_id.is_not(None))
    )).scalars().all()
    for sub in in_flight:
        result = await verify_remote_state(session, sub)
        stats.three_way_checked += 1
        if result == "verified":
            sub.status = "succeeded"
            stats.verified_recovered += 1
        elif result == "missing":
            await _abort_multipart_silently(...)
            _reset_to_pending(sub)
            stats.reset_to_pending += 1
        else:  # size_mismatch
            await _abort_multipart_silently(...)
            await _delete_object_silently(...)
            _reset_to_pending(sub)
            stats.size_mismatch_purged += 1

    # Step 2: reset status='assigned' with NO multipart_upload_id and stale assigned_at
    n = (await session.execute(
        update(FileSubTask)
        .where(FileSubTask.status == "assigned")
        .where(FileSubTask.multipart_upload_id.is_(None))
        .where(or_(FileSubTask.assigned_at.is_(None), FileSubTask.assigned_at < threshold))
        .values(status="pending", executor_id=None, executor_epoch=None,
                assignment_token=None, assigned_at=None)
    )).rowcount or 0
    stats.no_multipart_reset = n

    # Step 3: cleanup orphan multipart uploads (terminal status but mpu_id set)
    orphans = (await session.execute(
        select(FileSubTask)
          .where(FileSubTask.multipart_upload_id.is_not(None))
          .where(FileSubTask.status.in_(["succeeded", "failed", "cancelled"]))
    )).scalars().all()
    for sub in orphans:
        await _abort_multipart_silently(...)
        sub.multipart_upload_id = None
        stats.orphan_aborted += 1

    await session.commit()
    return stats
```

`verify_remote_state` Phase 1 head+size:

```python
async def verify_remote_state(session, sub) -> Literal["verified", "missing", "size_mismatch"]:
    storage_cfg = await _load_storage_config(session, sub)
    s3 = _make_s3_client(storage_cfg)
    key = _compose_key(sub, storage_cfg)
    try:
        head = await asyncio.to_thread(
            lambda: s3.head_object(Bucket=storage_cfg.bucket, Key=key)
        )
    except s3.exceptions.ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return "missing"
        raise
    remote_size = head.get("ContentLength", 0)
    if sub.file_size is not None and remote_size != sub.file_size:
        return "size_mismatch"
    return "verified"
```

---

## 7. Error Handling Matrix

| Source | Trigger | Handling | HTTP |
|--------|---------|----------|------|
| Executor request missing `X-Executor-Epoch` | dep `Header(...)` | FastAPI auto-422 → upgraded to 401 in dep | 401 |
| Executor epoch != DB epoch | `require_executor_epoch` | 401 `{code: EPOCH_MISMATCH, expected, got}` | 401 |
| Unknown executor_id | `require_executor_epoch` | 404 "executor not found" | 404 |
| `/report` epoch matches but token does not (reclaim happened) | `complete_subtask` raises ValueError | api translates to 409 `STALE_ASSIGNMENT` | 409 |
| Executor client receives 401 with `code=EPOCH_MISMATCH` | runner caught in `_poll_and_execute_loop` | abort in-flight + `_rejoin()` (fetches new epoch) + continue loop | n/a |
| Reclaim race: executor re-joined (epoch=N+1) before reclaim runs | `reclaim_subtasks` `WHERE executor_epoch=N` | rowcount=0; new epoch's work preserved | safe |
| Recovery routine head_object 5xx | `verify_remote_state` reraises | lifespan startup fails; controller does NOT serve traffic; operator investigates | startup abort |
| Recovery routine abort_multipart fails (mpu expired) | `_abort_multipart_silently` | swallow + log; S3 lifecycle is safety net | continue |
| Background `reclaim_loop` exception | outer `try/except` | `logger.exception` + continue next iteration | continue |
| Controller graceful shutdown | lifespan cleanup | `reclaim_task.cancel()` + 2s wait_for | clean exit |
| Concurrent `/join` for same executor_id | PG `ON CONFLICT DO UPDATE` | one UPDATE wins; both responses return distinct epochs | safe |

---

## 8. Testing Strategy

### 8.1 Unit + integration (CI required)

```
tests/auth/test_executor_epoch.py            [NEW]
  test_require_epoch_missing_header_401
  test_require_epoch_unknown_executor_404
  test_require_epoch_mismatch_returns_EPOCH_MISMATCH
  test_require_epoch_match_returns_executor_row

tests/services/test_executor_service.py      [MODIFY]
  test_join_first_time_returns_epoch_1
  test_join_existing_executor_increments_epoch
  test_join_concurrent_returns_distinct_epochs   # asyncio.gather × 2

tests/services/test_scheduler.py             [MODIFY]
  test_claim_writes_executor_epoch
  test_claim_assigned_at_set
  test_complete_subtask_rejects_stale_epoch
  test_reclaim_subtasks_resets_assigned
  test_reclaim_subtasks_skips_other_epoch
  test_reclaim_subtasks_skips_non_assigned

tests/services/test_recovery.py              [NEW]
  test_run_recovery_routine_resets_long_assigned
  test_run_recovery_routine_three_way_verified     # moto head match
  test_run_recovery_routine_three_way_missing      # moto NoSuchKey → reset
  test_run_recovery_routine_three_way_size_mismatch
  test_run_recovery_routine_aborts_orphan_multiparts
  test_reclaim_stale_executors_marks_unhealthy

tests/api/test_executors.py                  [MODIFY]
  test_heartbeat_missing_epoch_header_401
  test_heartbeat_wrong_epoch_returns_EPOCH_MISMATCH
  test_poll_correct_epoch_returns_assignment
  test_poll_after_rejoin_uses_new_epoch

tests/api/test_subtasks.py                   [MODIFY]
  test_report_with_stale_epoch_returns_409

tests/executor/test_client.py                [MODIFY]
  test_client_persists_epoch_from_join
  test_client_attaches_epoch_header
  test_client_on_401_epoch_mismatch_rejoins

tests/executor/test_runner.py                [MODIFY]
  test_runner_rejoins_on_epoch_mismatch       # FakeClient injects 401 once

tests/e2e/test_executor_e2e.py               [MODIFY]
  existing happy path exercises X-Executor-Epoch under the new dep
```

Estimated +30 tests on top of Phase 1's 99 → ~129.

### 8.2 No new CI jobs

`pytest` job picks up new tests automatically. `openapi-lint` already runs on
every PR (`spectral lint` + `swagger-cli validate`); P2-W1 spec changes are
covered by the existing job.

### 8.3 No new dev infra

`moto[s3]` (W4 dev dep) covers head_object + abort_multipart in tests.

---

## 9. Acceptance Criteria

- [ ] Single alembic migration adds 4 columns (executors.epoch, file_subtasks.{multipart_started_at, assigned_at, last_heartbeat_seen_at}); round-trip clean.
- [ ] 30+ new tests pass; **zero regressions** on the existing 99.
- [ ] `pnpm typecheck/lint/test:unit/build` still green (no frontend changes).
- [ ] Concurrent `/join` returns distinct, monotonically-increasing epochs (test verified via `asyncio.gather`).
- [ ] Background reclaim_loop verified via monkeypatched short interval + clock manipulation: stale executor → `unhealthy`, its subtasks → `pending`.
- [ ] Executor 401 EPOCH_MISMATCH → automatic rejoin + continued loop (no process exit).
- [ ] Three-way verification head+size covers verified / missing / size_mismatch via moto.
- [ ] `api/openapi.yaml` synced; `spectral lint` + `swagger-cli validate` both green.
- [ ] `docker-compose up -d --build` boots; lifespan recovery routine completes inside the controller's existing healthcheck timeout (5s for an empty DB; longer DB acceptable but documented).
- [ ] `pytest -m manual` smoke (if minio binary present) still passes after the protocol change.

---

## 10. Implementation Phasing (preview for plan)

| Milestone | Deliverable | Verification |
|-----------|-------------|--------------|
| M1 | Schema migration + `executors.epoch` model + `join_executor` atomic bump | `alembic` round-trip; concurrent join test |
| M2 | `require_executor_epoch` dep + endpoint wiring (heartbeat/poll/report) | endpoint tests (missing/mismatch/match) |
| M3 | `scheduler` updates: write epoch on claim + verify epoch on complete + `reclaim_subtasks` | scheduler tests |
| M4 | `recovery.py` module (run_recovery_routine + verify_remote_state + reclaim_stale_executors) | moto-based tests |
| M5 | Lifespan wiring (startup recovery + background reclaim loop) + ControllerClient + runner rejoin | runner test + e2e adaptation |
| M6 | OpenAPI sync + PR | CI 12/12 |

Plan estimate: **14-16 tasks** (similar to Week 4's 16). Largest of Phase 2 W1.

---

## 11. References

- `docs/v2.0/03-distributed-correctness.md` §2 (Fence Token / Epoch), §3 (Recovery), §5 (Reclaim semantics)
- `docs/v2.0/02-protocol.md` §6 (CAS-then-enqueue wire format)
- `docs/v2.0/08-mvp-roadmap.md` §2.6 Week 1 task breakdown
- Existing scheduler (extended): `src/dlw/services/scheduler.py`
- Existing executor service (extended): `src/dlw/services/executor_service.py`
- Existing client (extended): `src/dlw/executor/client.py`
- Phase 1 invariants 6/7/9/33/46 (`docs/v2.0/INVARIANTS.md`)
