# Phase 3 SP4 — CLI `dlw` + Python SDK (Design)

**Status:** approved (self-approved under the project's autonomous-execution directive)

> **Pre-execution review applied (2026-05-19, 2 opus reviewers).** Rulings folded into §7 + the plan: **(R1, BLOCKER)** `httpx==0.27.2` `ASGITransport` is async-only → sync `Client`/CLI tested via `httpx.MockTransport`, async `AsyncClient` via `ASGITransport` mirroring `tests/api/test_tasks.py` (no uvicorn live-server — cross-loop engine hazard). **(R2, BLOCKER)** `tests/sdk/_fixtures.py` must declare `__all__` (incl. the `_`-prefixed autouse fixtures) or `from … import *` silently drops them. **(R3, IMPORTANT)** `cancel_task` only ever sets `"cancelling"` synchronously (no executor in tests) → assert `== "cancelling"`. **(R4, IMPORTANT)** `--reason` is accepted but not persisted (no API field) — record as a known MVP limitation. **(R5, MINOR)** Task 11 must not hardcode the suite count / alembic head. Confirmed non-issues: returning `int` from the `dlw` entry point is sufficient (matches `dlw-executor`); `role="tenant_admin"` passes the tasks RBAC; submit→`TaskRead`/get→`TaskDetail` subtask shapes; argparse `--version` interception.
**Date:** 2026-05-19
**Sub-project:** Phase 3 SP4 (4th and final). SP1 #15, SP2 #16, SP3 #17 merged; `main` `b3b0b09`.
**Authoritative sources:** `docs/v2.0/11-cli-and-sdk-spec.md` (full vision), `docs/v2.0/08-mvp-roadmap.md` §3 (MVP acceptance), `api/openapi.yaml` + the implemented `src/dlw/api/tasks.py` (the real HTTP contract).

---

## 1. Goal & MVP scope

A Python SDK (`dlw.sdk`) and a `dlw` CLI that wrap the **already-implemented** controller REST API for the task lifecycle. Roadmap §3 acceptance: *CLI `dlw submit / list / show / cancel / watch` + Python SDK (sync + async)*.

SP4 is **purely additive**: it adds new packages (`dlw.sdk`, `dlw.cli.main`) and one `[project.scripts]` entry. It changes **no** controller endpoint, model, schema, migration, or lint rule — so its regression blast radius on the merged SP1–SP3 surface is near-zero.

### 1.1 In scope

- `dlw.sdk.Client` (sync) + `dlw.sdk.AsyncClient` (async), each exposing a `tasks` API:
  - `submit(repo_id, revision, *, storage_id=None, priority=1, source_strategy="auto_balance", source_blacklist=None, file_filter="core_only", file_glob=None, upgrade_from_revision=None, download_bytes_limit=None, trust_non_hf_sha256=False) -> DownloadTask`
  - `get(task_id) -> DownloadTask`
  - `list(*, status=None, limit=50) -> list[DownloadTask]`
  - `cancel(task_id, reason=None) -> None`
  - `delete(task_id) -> None` (SP3 `DELETE /api/v1/tasks/{id}`)
  - `DownloadTask.wait(timeout=None, on_progress=None, poll_interval=5.0) -> DownloadTask` and `.refresh() -> DownloadTask` (async variants on `AsyncDownloadTask`)
- `dlw` CLI subcommands: `submit`, `list`, `show`, `cancel`, `delete`, `watch` — thin wrappers that build an SDK `Client` and render results (CLI-is-SDK, spec §7).
- Global CLI flags: `--server`, `--token`, `-o/--output {table,json}`, `-c/--config`, `-q/--quiet`, `--version`, `-h/--help`.
- POSIX exit codes per spec §4.1 (see §6).
- Typed SDK error hierarchy (`dlw.sdk.errors`) mapped from API HTTP status + error `code`.

### 1.2 Explicitly OUT of scope (deferred; recorded so the plan does not creep)

OIDC / device-code `login`/`logout`/`whoami`; WebSocket/SSE `stream_events` & `events`; `materialize` (needs executor/storage byte path, not API-only); `retry`/`upgrade`/`replan` CLI (no implemented endpoints); `search`/`info`/`quota`/`exec`/`storage`/`audit`/`template`/`admin`/`completion`; `--idempotency-key` (API has no idempotency key); `--output yaml|wide`; Rich/Typer; multi-context config management beyond reading one config file; server-side `list` filtering (done client-side — see §4).

These are future sub-projects / Phase 4. The SDK/CLI public surface added here is forward-compatible with adding them later.

---

## 2. Architecture

```
src/dlw/sdk/
  __init__.py      # exports Client, AsyncClient, errors, DownloadTask, models
  _config.py       # resolve(server, token, config_path) precedence + ~/.dlw/config.yaml read
  _http.py         # shared: build httpx kwargs, response -> typed error mapping
  errors.py        # DlwError + NotFound/AuthError/QuotaExceeded/Conflict/UsageError/TimeoutError/ApiError
  models.py        # thin response models (reuse dlw.schemas.task DTOs for parse) + DownloadTask wrapper
  client.py        # Client (sync, httpx.Client) + TasksAPI + DownloadTask.wait/refresh
  aclient.py       # AsyncClient (httpx.AsyncClient) + AsyncTasksAPI + AsyncDownloadTask
src/dlw/cli/
  main.py          # argparse parser + subcommand handlers + render + exit-code mapping; main()->int
```

- **Package boundary (deviation from spec §6.2):** the spec shows `from dlw import Client`. In this monorepo `dlw` is the controller package; exposing the client at top level would heavy-import FastAPI/SQLAlchemy. SP4 uses `from dlw.sdk import Client, AsyncClient`. When the SDK is later split into its own published distribution it can re-export as `dlw`. This deviation is intentional and recorded.
- **CLI is SDK:** every CLI handler constructs a `Client` and calls SDK methods (spec §7) — one code path, guaranteed consistency.
- **Sync/async share** `_config.py`, `_http.py`, `errors.py`, `models.py`. Only the transport (`httpx.Client` vs `httpx.AsyncClient`) and the `wait`/`refresh` coroutine wrappers differ. No logic duplicated beyond the unavoidable sync/async method shells.

### 2.1 Config & precedence

- **server:** `--server` flag > `DLW_SERVER` > config `contexts.<current>.server` > `http://localhost:8000`.
- **token:** `--token` flag > `DLW_TOKEN` > `DLW_SYSTEM_ADMIN_TOKEN` > config `auth.<current>.access_token`.
- Config file: `--config`/`DLW_CONFIG` > `$XDG_CONFIG_HOME/dlw/config.yaml` > `~/.dlw/config.yaml`. **Missing config is not an error** (env/flags suffice — the CI/non-interactive path, which is the only auth path SP1 supports). `pyyaml` is already a dependency.
- Auth header: `Authorization: Bearer <token>`. A missing token yields a usage error (exit 2) *before* any HTTP call.

---

## 3. Data flow

`dlw submit org/m -r <sha>` → CLI parses args → `Client(server, token)` → `client.tasks.submit(...)` → `POST /api/v1/tasks` with JSON body matching `TaskCreate` → 201 `TaskRead` → wrap in `DownloadTask` → render (table: key columns; json: the raw API JSON, the stable contract) → exit 0.

`dlw watch <id>` → `client.tasks.get(id)` then loop `task.refresh()` every `poll_interval` printing progress until `status in {succeeded, failed, cancelled}` or `--timeout`; Ctrl-C → exit 8; timeout → exit 9; terminal `failed` → exit 1; success → exit 0.

`client.tasks.list(status="downloading")` → `GET /api/v1/tasks` → `TaskList{items,total}` → client-side filter by `status` (the implemented endpoint has no query filter; documented MVP limitation, server-side filter deferred) → `list[DownloadTask]`.

---

## 4. Known MVP limitations (authoritative — deferred on purpose)

1. **`list` filtering is client-side.** `GET /api/v1/tasks` returns all tenant tasks ordered by `created_at desc`; the SDK/CLI applies `status`/`limit` in the client. Correct for MVP scale; a server-side `?status=&limit=&cursor=` is a future additive controller change.
2. **`watch`/`wait` is polling, not streaming.** No events/WS endpoint is implemented. `poll_interval` default 5s. `stream_events` is deferred.
3. **Token-only auth.** No OIDC; the CLI/SDK consume a pre-existing system-JWT (e.g. `DLW_SYSTEM_ADMIN_TOKEN`) exactly as SP1 intended for non-interactive use.

These are safe: each fails or degrades loudly within bounded behavior and none blocks the roadmap §3 acceptance.

---

## 5. Error handling

`dlw.sdk.errors`:

| Class | Raised when | CLI exit |
|-------|-------------|----------|
| `UsageError` | bad args / missing token (pre-flight) | 2 |
| `NotFound` | HTTP 404 | 3 |
| `AuthError` | HTTP 401/403 | 4 |
| `QuotaExceeded` | HTTP 429 or code `QUOTA_EXCEEDED` | 5 |
| `Conflict` | HTTP 409 (e.g. `TASK_NOT_TERMINAL`, duplicate) | 6 |
| `TimeoutError` | `wait`/`watch` exceeded `--timeout` | 9 |
| `ApiError` | other non-2xx (carries status, code, trace_id, details) | 1 |
| `DlwError` | base of all the above | 1 |

`_http.raise_for_status(resp)` centralizes the mapping (status → class; body `{code,message,trace_id,details}` parsed when JSON, tolerated when not). The CLI catches `DlwError`, prints the spec §4.2 stderr block (`Error:`/`Code:`/`Trace:` + `Details:`; `-o json` → a JSON object on stderr), and returns the mapped exit code. `KeyboardInterrupt` → exit 8.

---

## 6. Exit codes (spec §4.1, implemented)

0 success · 1 generic/unexpected (incl. `failed` task under `--wait`) · 2 usage · 3 not-found · 4 auth/forbidden · 5 quota/rate · 6 conflict · 8 user-cancel (SIGINT) · 9 timeout. (7 upstream-degraded reserved, not emitted by the MVP surface.)

`main(argv=None) -> int`; the console-script wrapper does `raise SystemExit(main())`.

---

## 7. Testing strategy

- **Async e2e (real app, no network, no new dep):** the **async** `AsyncClient` is tested against the real ASGI app via `httpx.ASGITransport(app=make_app_with_state(...))` using the exact proven pattern of `tests/api/test_tasks.py` (async test funcs under `asyncio_mode=auto` + an *async, function-scoped* fixture that builds the client — same event loop as the session-scoped `engine`). This exercises the true SDK→FastAPI→service→DB path, catching real contract drift.
  - **Pinned-httpx constraint (pre-review BLOCKER):** `httpx==0.27.2`'s `ASGITransport` is **async-only** (`AsyncBaseTransport`; no `handle_request`). A sync `httpx.Client` therefore **cannot** drive `ASGITransport`. So the **sync `Client` and the CLI** (which builds a sync `Client`) are tested with `httpx.MockTransport` (an httpx built-in that *is* sync-compatible) returning realistic FastAPI-shaped responses from a tiny in-memory task store (`tests/sdk/_mock.py`). The real-API-contract risk is owned by the async e2e — `_config`/`_http`/path/body/error-mapping code is shared verbatim by both clients; the only sync/async delta is `httpx.Client.post` vs `await httpx.AsyncClient.post`. Both `Client`/`AsyncClient` accept an optional injected `transport=` (MockTransport for sync/CLI, ASGITransport for async); production callers never pass it. No uvicorn live-server thread is used (it would reintroduce the recurring cross-event-loop cached-engine hazard in the full suite).
- **Coverage:** submit (201→DownloadTask), get, list (+client-side status filter), cancel (202), delete (204) and delete-non-terminal→`Conflict`/exit 6, cross-tenant/404→`NotFound`/exit 3, missing-token→`UsageError`/exit 2, `wait` reaching terminal via polled status (fake/short interval), error-mapping table, CLI json output is parseable & table output non-empty, `main()` exit codes.
- **Conventions (baked-in lessons):** new test dirs get `__init__.py`; any DB-touching fixture uses `drop_all→create_all` + teardown `drop_all`, seeds **all FK parent rows** (Tenant→Project/User/StorageBackend) with an intermediate `await s.flush()` before child rows (SP3 fixture-FK lesson); reuse the SP1 app/token helper rather than re-rolling JWT minting.
- **Real CI gates only:** pytest (full suite at the milestone boundary), `tools/lint_invariants.py` (+ its pytest), `tools/lint_no_direct_status_write.py`, openapi spectral+swagger-cli (unchanged — SP4 touches no API yaml), yamllint `api/` (unaffected). **No new runtime or dev dependency** → `uv` lock and `[dependency-groups]` unchanged; argparse + httpx + pyyaml + pydantic are all already present.

---

## 8. Milestones (for the plan)

- **M1 — SDK core:** `_config`, `_http`, `errors`, `models`, sync `Client.tasks` (submit/get/list/cancel/delete) + `DownloadTask.refresh`. Tests: in-process e2e for each + error mapping.
- **M2 — wait + async:** `DownloadTask.wait`/`on_progress`/timeout; `AsyncClient`/`AsyncTasksAPI`/`AsyncDownloadTask` mirroring + async tests.
- **M3 — CLI:** `dlw.cli.main` argparse, subcommands, table/json render, exit-code mapping, `[project.scripts] dlw=`; CLI tests (json parse + exit codes). Full-suite milestone gate + all CI gates.
- **M4 — docs + PR:** operator/user doc `docs/operator/cli-sdk.md` (install caveat per spec §1 banner, the 3 MVP limitations, auth env, exit codes, examples, deferred list); final opus whole-impl review (mandatory — new console entrypoint + new public package surface); push + PR + CI-wait + squash-merge.

---

## 9. Self-review

- **Placeholders:** none — every component, signature, precedence rule, error→exit mapping, and test approach is concrete.
- **Internal consistency:** CLI exit codes (§6) match the error table (§5); architecture (§2) matches the milestones (§8); the "purely additive / no controller change" claim holds (only new files + one `[project.scripts]` line).
- **Scope:** single focused plan; the large v2.0 vision is decomposed — only the roadmap-§3 MVP is in, everything else explicitly deferred in §1.2 with rationale.
- **Ambiguity:** resolved explicitly — package path (`dlw.sdk`, not `dlw`), framework (argparse), watch (polling), auth (token-only), list filter (client-side), output (table/json), test transport (ASGITransport). Each ambiguous fork was decided toward the conservative/lowest-blast-radius option.
- **Risk:** lowest of the four sub-projects — no migration, no model/relationship change, no scheduler/lifespan touch; the only cross-cutting artifact is one new console-script entry. Final whole-impl review still required (new public surface + entrypoint), per the SP1 "production-only wiring" lesson.
