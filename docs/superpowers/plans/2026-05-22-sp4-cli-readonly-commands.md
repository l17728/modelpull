# SP4 CLI — Deferred Read-Only Commands Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add read-only `dlw` commands (`whoami`, `quota`, `exec list`, `events`, `audit`) + matching SDK methods, wrapping controller endpoints that already accept Bearer auth — purely additive, no backend change.

**Architecture:** New SDK resource classes (`QuotaAPI`/`ExecutorsAPI`/`AuditAPI`) + `Client.me()` + `TasksAPI.events()`, mirrored on `AsyncClient`; each returns the parsed JSON dict. New argparse subcommands dispatch to handlers that render via a generic `_emit_obj`. `events --follow` streams the existing SSE endpoint.

**Tech Stack:** Python 3.12, httpx (sync + async), argparse; pytest asyncio_mode=auto, httpx MockTransport + ASGITransport.

**Spec:** `docs/superpowers/specs/2026-05-22-sp4-cli-readonly-commands-design.md` (read fully — the scope table, the deferred-with-reason list, the mixed-return-type note).

**Locked constraints (do NOT violate):**
- Purely additive: do NOT modify `submit/list/show/cancel/delete/watch` or their tests. New SDK namespaces + new subcommands only.
- New read-only SDK methods return the parsed JSON dict (`r.json()`), NOT new model classes (read-only metadata; consistent with the endpoints' response shapes).
- Audit query params: the controller uses `actor_user_id`, `action`, `from` (FastAPI `from_` with `alias="from"`), `to`, `cursor`, `limit` (verified `api/audit.py:26-29`). The SDK kwarg is `from_` (Python keyword) → send query KEY `from`.
- `exec` is a nested subcommand (`dlw exec list`); if the flat argparse structure makes nesting awkward, use `dlw exec` defaulting to list — pick the cleaner form and document it.
- No backend change, no migration, no openapi change, no new dep, no frontend. `login`/device-flow, config-write, materialize, retry/upgrade, storage/template/admin stay deferred (no endpoints).
- CI doesn't gate ruff — real gate is `uv run pytest` + `python tools/lint_invariants.py [--strict]`; `ruff --select I001 --fix` new files only.

---

## File Structure

- **Modify** `src/dlw/sdk/client.py` — `QuotaAPI`/`ExecutorsAPI`/`AuditAPI`, `TasksAPI.events`, `Client.me` + wire namespaces.
- **Modify** `src/dlw/sdk/aclient.py` — async mirrors.
- **Modify** `src/dlw/cli/main.py` — subparsers + `_emit_obj`/`_emit_rows` + pass `_emit_obj` to handlers.
- **Modify** `src/dlw/cli/handlers.py` — new command branches + `events --follow`.
- **Modify** `tests/sdk/_mock.py` — new endpoint handlers.
- **Create** `tests/cli/test_cli_readonly.py`; **Modify** `tests/sdk/test_client_async.py`.
- **Modify** `docs/operator/cli-sdk.md`.

---

## Milestone M1 — SDK

### Task 1: Sync SDK methods + mock handlers + sync tests

**Files:**
- Modify: `src/dlw/sdk/client.py`, `tests/sdk/_mock.py`
- Test: `tests/sdk/test_client_sync_ops.py` (extend) or a new `tests/sdk/test_readonly.py`

- [ ] **Step 1: Add mock endpoint handlers.** In `tests/sdk/_mock.py`, read the existing `handler()` routing, then add routes returning realistic JSON:
  - `GET /api/v1/auth/me` → `{"user_id":1,"tenant_id":1,"role":"tenant_admin","project_ids":[1],"is_service":false}`
  - `GET /api/v1/quota/current` → `{"tenant_id":1,"bytes_used_month":0,"bytes_quota_month":1000,"storage_gb_used":0,"storage_gb_quota":1024,"concurrent_tasks":0,"concurrent_quota":10}`
  - `GET /api/v1/executors` → `{"items":[{"id":"ex-1","status":"healthy","health_score":100,"epoch":1,"host_id":"h1","tenant_id":1,"last_heartbeat_at":null,"nic_speed_gbps":10,"disk_free_gb":900,"disk_total_gb":1000,"created_at":"2026-01-01T00:00:00"}]}` (respect a `?status=` filter if present)
  - `GET /api/v1/tasks/{id}/events` → `{"items":[{"id":1,"occurred_at":"2026-01-01T00:00:00","action":"task.created","message":"created","outcome":"success"}],"next_cursor":null}`
  - `GET /api/v1/audit/log` → `{"items":[{"id":1,"occurred_at":"2026-01-01T00:00:00","tenant_id":1,"actor_user_id":1,"action":"task.created","resource_type":"task","resource_id":"t1","outcome":"success","payload":{}}],"next_cursor":null}`

- [ ] **Step 2: Write the failing sync tests.** In `tests/sdk/test_readonly.py` (new; mirror `test_client_sync.py`'s `Client(..., transport=make_mock_transport())` setup):
```python
def test_me(sync_client):
    assert sync_client.me()["role"] == "tenant_admin"

def test_quota(sync_client):
    q = sync_client.quota.current()
    assert q["bytes_quota_month"] == 1000 and "storage_gb_used" in q

def test_executors_list(sync_client):
    items = sync_client.executors.list()["items"]
    assert items and items[0]["id"] == "ex-1"

def test_task_events(sync_client):
    ev = sync_client.tasks.events("11111111-1111-1111-1111-111111111111")
    assert ev["items"][0]["action"] == "task.created"

def test_audit_search(sync_client):
    a = sync_client.audit.search(action="task.")
    assert a["items"][0]["outcome"] == "success"
```
(Define a `sync_client` fixture building `Client(server="http://mock", token="good", transport=make_mock_transport())` — copy the exact construction from the existing sync test file.)

- [ ] **Step 3: Verify FAIL** (`cd "D:/download_weights" && uv run pytest tests/sdk/test_readonly.py -v`) — methods/namespaces missing.

- [ ] **Step 4: Implement in `src/dlw/sdk/client.py`.** Add the three resource classes (before `class Client`):
```python
class QuotaAPI:
    def __init__(self, http: httpx.Client) -> None:
        self._h = http
    def current(self) -> dict:
        r = self._h.get("/api/v1/quota/current")
        raise_for_status(r)
        return r.json()


class ExecutorsAPI:
    def __init__(self, http: httpx.Client) -> None:
        self._h = http
    def list(self, *, status: str | None = None) -> dict:
        params = {"status": status} if status else None
        r = self._h.get("/api/v1/executors", params=params)
        raise_for_status(r)
        return r.json()


class AuditAPI:
    def __init__(self, http: httpx.Client) -> None:
        self._h = http
    def search(self, *, action: str | None = None,
               actor_user_id: int | None = None, from_: str | None = None,
               to: str | None = None, cursor: str | None = None,
               limit: int = 50) -> dict:
        params: dict[str, Any] = {"limit": limit}
        if action is not None: params["action"] = action
        if actor_user_id is not None: params["actor_user_id"] = actor_user_id
        if from_ is not None: params["from"] = from_      # query KEY is 'from'
        if to is not None: params["to"] = to
        if cursor is not None: params["cursor"] = cursor
        r = self._h.get("/api/v1/audit/log", params=params)
        raise_for_status(r)
        return r.json()
```
Add to `TasksAPI`:
```python
    def events(self, task_id: str, *, limit: int = 50,
               cursor: str | None = None) -> dict:
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None: params["cursor"] = cursor
        r = self._h.get(f"/api/v1/tasks/{task_id}/events", params=params)
        raise_for_status(r)
        return r.json()
```
Add to `Client.__init__` (after `self.tasks = TasksAPI(self._http)`):
```python
        self.quota = QuotaAPI(self._http)
        self.executors = ExecutorsAPI(self._http)
        self.audit = AuditAPI(self._http)
```
Add a `Client.me` method:
```python
    def me(self) -> dict:
        r = self._http.get("/api/v1/auth/me")
        raise_for_status(r)
        return r.json()
```

- [ ] **Step 5: Verify PASS** + sync regression: `cd "D:/download_weights" && uv run pytest tests/sdk/test_readonly.py tests/sdk/test_client_sync.py -v` → all pass.

- [ ] **Step 6: Tidy + commit.**
```bash
cd "D:/download_weights" && uv run ruff check --select I001 --fix src/dlw/sdk/client.py tests/sdk/_mock.py tests/sdk/test_readonly.py
git add src/dlw/sdk/client.py tests/sdk/_mock.py tests/sdk/test_readonly.py && git commit -m "feat(sp4cli): sync SDK me/quota/executors/audit/tasks.events"
```

### Task 2: Async SDK mirrors + async tests

**Files:**
- Modify: `src/dlw/sdk/aclient.py`
- Test: `tests/sdk/test_client_async.py` (extend)

- [ ] **Step 1: Read `aclient.py`** to match its exact async resource-class pattern (`AsyncTasksAPI` etc.). Add async `AsyncQuotaAPI`/`AsyncExecutorsAPI`/`AsyncAuditAPI` (same methods, `async def` + `await self._h.get(...)`), `AsyncTasksAPI.events`, `AsyncClient.me`, and wire `self.quota/executors/audit` in `AsyncClient.__init__`.

- [ ] **Step 2: Extend `tests/sdk/test_client_async.py`** (uses the real ASGI app via the `aclient` fixture). Add (the `_bootstrap` already seeds tenant/quota/executor — verify; if an audit row or task-events row is needed, seed minimally or assert on shape/empty):
```python
async def test_me_async(aclient):
    assert (await aclient.me())["tenant_id"] == 1

async def test_quota_async(aclient):
    assert "bytes_quota_month" in await aclient.quota.current()

async def test_executors_async(aclient):
    assert "items" in await aclient.executors.list()

async def test_audit_async(aclient):
    assert "items" in await aclient.audit.search(limit=10)
```
(For `tasks.events`, if the bootstrap creates a task you can reference, assert its events shape; else assert a known task id returns `{"items":[...]}` or a 404→NotFound. Keep assertions shape-based to avoid coupling to seeded data.)

- [ ] **Step 3: Verify PASS** (`cd "D:/download_weights" && uv run pytest tests/sdk/test_client_async.py -v`) → all pass.

- [ ] **Step 4: Tidy + commit.**
```bash
cd "D:/download_weights" && uv run ruff check --select I001 --fix src/dlw/sdk/aclient.py tests/sdk/test_client_async.py
git add src/dlw/sdk/aclient.py tests/sdk/test_client_async.py && git commit -m "feat(sp4cli): async SDK mirrors for me/quota/executors/audit/events"
```

### Task 3: M1 backend gate

- [ ] `cd "D:/download_weights" && uv run pytest -q` → all pass. `python tools/lint_invariants.py --strict` → OK. No commit.

---

## Milestone M2 — CLI

### Task 4: CLI subcommands + generic emitter + handlers

**Files:**
- Modify: `src/dlw/cli/main.py`, `src/dlw/cli/handlers.py`
- Test: `tests/cli/test_cli_readonly.py`

- [ ] **Step 1: Write the failing CLI tests.** Create `tests/cli/test_cli_readonly.py` (mirror `tests/cli/test_cli_ops.py`'s `_wire` autouse fixture that sets `dlw.cli.main._transport = make_mock_transport()` + `DLW_TOKEN`/`DLW_SERVER`, and calls `cli.main([...])` capturing `capsys`):
```python
def test_whoami(capsys):
    assert cli.main(["whoami"]) == 0
    out = capsys.readouterr().out
    assert "tenant_admin" in out

def test_quota_json(capsys):
    assert cli.main(["-o", "json", "quota"]) == 0
    import json
    d = json.loads(capsys.readouterr().out)
    assert d["bytes_quota_month"] == 1000

def test_exec_list(capsys):
    assert cli.main(["exec", "list"]) == 0
    assert "ex-1" in capsys.readouterr().out

def test_events(capsys):
    assert cli.main(["events", "11111111-1111-1111-1111-111111111111"]) == 0
    assert "task.created" in capsys.readouterr().out

def test_audit(capsys):
    assert cli.main(["audit", "--action", "task."]) == 0
    assert "task.created" in capsys.readouterr().out
```
(Copy the exact `_wire`/import setup from `test_cli_ops.py`. If `exec list` nested parsing needs a different invocation, adjust to the form you implement and keep the assertion.)

- [ ] **Step 2: Verify FAIL** (`cd "D:/download_weights" && uv run pytest tests/cli/test_cli_readonly.py -v`) — unknown commands → exit 2.

- [ ] **Step 3: Add subparsers** in `main.py::_build_parser` (after the `watch` parser, before `return p`):
```python
    sub.add_parser("whoami", help="show the current principal")
    sub.add_parser("quota", help="show current tenant quota usage")

    ex = sub.add_parser("exec", help="executor commands")
    ex_sub = ex.add_subparsers(dest="exec_cmd")
    ex_ls = ex_sub.add_parser("list", help="list executors")
    ex_ls.add_argument("--status", default=None)

    ev = sub.add_parser("events", help="show task events")
    ev.add_argument("task_id")
    ev.add_argument("--limit", type=int, default=50)
    ev.add_argument("--cursor", default=None)
    ev.add_argument("--follow", action="store_true",
                    help="stream events via SSE until the task is terminal")

    au = sub.add_parser("audit", help="search the audit log")
    au.add_argument("--action", default=None)
    au.add_argument("--actor", type=int, default=None)
    au.add_argument("--from", dest="from_", default=None)
    au.add_argument("--to", default=None)
    au.add_argument("--limit", type=int, default=50)
    au.add_argument("--cursor", default=None)
```

- [ ] **Step 4: Add the generic emitter** in `main.py` (after `_emit`):
```python
def _emit_obj(obj: Any, args: argparse.Namespace) -> None:
    if args.output == "json":
        sys.stdout.write(json.dumps(obj, default=str) + "\n")
        return
    if isinstance(obj, dict) and isinstance(obj.get("items"), list):
        _emit_rows(obj["items"])
    elif isinstance(obj, list):
        _emit_rows(obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            sys.stdout.write(f"{k}: {v}\n")
    else:
        sys.stdout.write(str(obj) + "\n")


def _emit_rows(rows: list) -> None:
    if not rows:
        sys.stdout.write("(none)\n")
        return
    cols: list[str] = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    widths = {c: max(len(c), max(len(str(r.get(c, ""))) for r in rows))
              for c in cols}
    sys.stdout.write("  ".join(c.ljust(widths[c]) for c in cols) + "\n")
    for r in rows:
        sys.stdout.write("  ".join(
            str(r.get(c, "")).ljust(widths[c]) for c in cols) + "\n")
```
And pass it to handlers — change `_dispatch`:
```python
def _dispatch(args: argparse.Namespace) -> int:
    from dlw.cli import handlers
    return handlers.run(args, _make_client, _emit, _emit_obj)
```

- [ ] **Step 5: Add handler branches** in `handlers.py`. Change the signature to `def run(args, make_client, emit, emit_obj) -> int:` and add branches (inside the `try`, before `raise NotImplementedError`):
```python
        if args.cmd == "whoami":
            emit_obj(client.me(), args)
            return 0
        if args.cmd == "quota":
            emit_obj(client.quota.current(), args)
            return 0
        if args.cmd == "exec":
            if getattr(args, "exec_cmd", None) == "list":
                emit_obj(client.executors.list(status=args.status), args)
                return 0
            sys.stderr.write("usage: dlw exec list\n")
            return 2
        if args.cmd == "events":
            if args.follow:
                return _follow_events(client, args)
            emit_obj(client.tasks.events(
                args.task_id, limit=args.limit, cursor=args.cursor), args)
            return 0
        if args.cmd == "audit":
            emit_obj(client.audit.search(
                action=args.action, actor_user_id=args.actor,
                from_=args.from_, to=args.to, limit=args.limit,
                cursor=args.cursor), args)
            return 0
```
And add the SSE follow helper (uses the client's underlying httpx for streaming):
```python
def _follow_events(client, args) -> int:
    import json
    url = f"/api/v1/tasks/{args.task_id}/events/stream"
    with client._http.stream("GET", url) as r:
        if r.status_code != 200:
            r.read()
            from dlw.sdk._http import raise_for_status
            raise_for_status(r)
        for line in r.iter_lines():
            if line.startswith("data: "):
                sys.stdout.write(line[len("data: "):] + "\n")
                sys.stdout.flush()
    return 0
```
(`client._http` is the httpx client; using it for streaming is acceptable for the CLI layer. If a cleaner SDK seam is preferred, add `TasksAPI.events_stream(task_id)` returning the stream context manager — but the direct `_http.stream` keeps scope minimal. The stream self-terminates when the task is terminal.)

- [ ] **Step 6: Verify PASS** + full CLI regression: `cd "D:/download_weights" && uv run pytest tests/cli/ -v` → all pass (existing `test_cli_ops.py` etc. unaffected — `run`'s new 4th param is additive; `_dispatch` always passes it).

- [ ] **Step 7: `--follow` test** (best-effort). Add a test that drives `events --follow` against a mock SSE stream if the mock transport can return an event-stream body; if awkward with MockTransport, add an async test in `tests/sdk/`/`tests/cli/` against the real `/events/stream` ASGI endpoint with `?max_ticks=1` (the SSE endpoints support that test hatch — verify) so it terminates. If neither is clean, assert `_follow_events` parses a `data:` line from a hand-built mock response and document the limitation. Do NOT block the milestone on an elaborate streaming test.

- [ ] **Step 8: Tidy + commit.**
```bash
cd "D:/download_weights" && uv run ruff check --select I001 --fix src/dlw/cli/main.py src/dlw/cli/handlers.py tests/cli/test_cli_readonly.py
git add src/dlw/cli/main.py src/dlw/cli/handlers.py tests/cli/test_cli_readonly.py && git commit -m "feat(sp4cli): dlw whoami/quota/exec list/events/audit commands"
```

### Task 5: M2 backend gate

- [ ] `cd "D:/download_weights" && uv run pytest -q` → all pass. `python tools/lint_invariants.py --strict` → OK. No commit.

---

## Milestone M3 — docs

### Task 6: Update operator CLI docs

**Files:**
- Modify: `docs/operator/cli-sdk.md`

- [ ] **Step 1:** In `docs/operator/cli-sdk.md`, document the new commands (`whoami`, `quota`, `exec list`, `events [--follow]`, `audit`) with one-line usage each + the SDK methods (`client.me()`, `client.quota.current()`, `client.executors.list()`, `client.tasks.events()`, `client.audit.search()`). Move these out of the §6 "deferred" list; keep `login`/`logout`/`materialize`/`retry`/`upgrade`/`search`/`info`/`storage`/`template`/`admin`/`completion`/config-write in deferred WITH the reason (`login` needs a device-code flow endpoint that doesn't exist; the rest have no implemented endpoints). Prose; match the file's style.

- [ ] **Step 2: Commit.**
```bash
cd "D:/download_weights" && git add docs/operator/cli-sdk.md && git commit -m "docs(sp4cli): document read-only CLI commands; trim deferred list"
```

---

## Self-Review

**1. Spec coverage:** §1 scope table → Tasks 1,2,4 ✓; §2 SDK → Tasks 1,2 ✓; §3 CLI → Task 4 ✓; §4 tests → Tasks 1,2,4 ✓; §5 milestones → M1/M2/M3 ✓.

**2. Placeholder scan:** Task 2 Step 2 / Task 4 Step 7 specify implementer-judgment outcomes (seed-or-shape-assert; `--follow` test fallback) with concrete acceptable results, not TODOs. The `exec` nested-subparser form is decided (`dlw exec list`). No vague gaps.

**3. Type consistency:** SDK read-only methods return `dict`; kwargs `status`/`action`/`actor_user_id`/`from_`(→`from`)/`to`/`cursor`/`limit`; `tasks.events(task_id, *, limit, cursor)`; `Client.me()`. `handlers.run(args, make_client, emit, emit_obj)` (4-arg). `_emit_obj`/`_emit_rows` generic. Consistent across tasks.

**Open risks for reviewers:** (a) `client._http.stream(...)` for `events --follow` — is reaching into the SDK's private httpx acceptable, or should a `TasksAPI.events_stream` seam be added? (b) does the async `_bootstrap` in `test_client_async.py` already seed a quota snapshot + an executor so `quota.current()`/`executors.list()` return non-empty (or do they need seeding / shape-only assertions)? (c) the `--from`/`dest=from_` argparse + SDK `from_`→`from` query mapping — correct end-to-end? (d) does `GET /api/v1/auth/me` work with the system/static token the SDK uses (require_principal), or only with an OIDC-issued JWT?