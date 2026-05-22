# SP4 CLI — Deferred Read-Only Commands (Design)

> Closes the highest-value, lowest-risk slice of the deferred `dlw` CLI/SDK
> surface (v2.0 §11): **read-only command wraps** of controller endpoints that
> already accept token (Bearer) auth — `whoami`, `quota`, `exec list`,
> `events`, `audit`. Purely additive: new SDK resource namespaces + new CLI
> subcommands; the existing `submit/list/show/cancel/delete/watch` are untouched.
> Status: self-approved per Rule #1. Branch: `feat/sp4-cli-readonly-commands`.

## 1. Scope

**In scope (additive; no backend change, no migration, no new dep):**

New SDK methods + CLI subcommands wrapping existing token-auth GET endpoints:

| CLI | SDK | Endpoint | Auth |
|-----|-----|----------|------|
| `dlw whoami` | `client.me()` | `GET /api/v1/auth/me` | `require_principal` (any bearer) |
| `dlw quota` | `client.quota.current()` | `GET /api/v1/quota/current` | `require_perm` (all tenant roles) |
| `dlw exec list [--status S]` | `client.executors.list(status=None)` | `GET /api/v1/executors` | `require_perm` |
| `dlw events <id> [--limit N] [--cursor C] [--follow]` | `client.tasks.events(task_id, *, limit=50, cursor=None)` | `GET /api/v1/tasks/{id}/events` (+ `/events/stream` for `--follow`) | `require_perm` tasks |
| `dlw audit [--action P] [--actor U] [--from D] [--to D] [--limit N] [--cursor C]` | `client.audit.search(...)` | `GET /api/v1/audit/log` | `require_perm` (all roles) |

Each new SDK method returns the **parsed JSON dict** (read-only metadata — no new
typed model classes; consistent with `quota_read`/`executor_read`/`audit`
response shapes). Async mirrors on `AsyncClient` for all of them.

CLI output: the existing `_emit` only formats `DownloadTask`-shaped rows. Add a
generic `_emit_obj(obj, args)` that (a) `-o json` → `json.dumps`, (b) table mode
→ key:value lines for a dict, or a column table for a list-of-dicts (whatever the
endpoint returns). Reuse for all new commands. `events --follow` streams SSE
lines (httpx `iter_lines`) printing each event JSON until the stream closes
(terminal task) or Ctrl-C.

**Out of scope (named, deferred — needs new backend or is browser-only):**
- `dlw login` / `logout` — the controller's OIDC is a **browser authorization-code
  redirect flow** (`/auth/login` → IdP → `/auth/callback`); a CLI needs a
  **device-code flow** (RFC 8628) endpoint (`POST /auth/device`) that **does not
  exist**. Out of scope (would require new controller support).
- `dlw config set/edit` — the config-read path already works (`_config.py`);
  the write-back path (storing a token, switching context) is deferred (it pairs
  naturally with `login`).
- `materialize`, `retry`, `upgrade`, `search`, `info`, `storage`, `template`,
  `admin`, `completion`, `--idempotency-key`, `-o yaml|wide`, Rich/Typer — no
  implemented endpoints / out of this slice.
- **`watch` SSE upgrade** — `watch` (task-status poller) keeps polling; it works
  and changing it is regression risk. Live streaming is delivered additively via
  `events --follow` instead. A `watch`→SSE upgrade is a named follow-on.

## 2. SDK extension (`src/dlw/sdk/`)

Follow the existing `TasksAPI` pattern (`client.py`): a resource class holding the
httpx client, methods call it + `raise_for_status` + return `r.json()`.

`client.py` (sync) — new classes + wire into `Client.__init__`:
```python
class QuotaAPI:
    def __init__(self, http): self._h = http
    def current(self) -> dict:
        r = self._h.get("/api/v1/quota/current"); raise_for_status(r); return r.json()

class ExecutorsAPI:
    def __init__(self, http): self._h = http
    def list(self, *, status: str | None = None) -> dict:
        params = {"status": status} if status else None
        r = self._h.get("/api/v1/executors", params=params)
        raise_for_status(r); return r.json()

class AuditAPI:
    def __init__(self, http): self._h = http
    def search(self, *, action=None, actor_user_id=None, from_=None, to=None,
               cursor=None, limit=50) -> dict:
        params = {k: v for k, v in {
            "action": action, "actor_user_id": actor_user_id, "from": from_,
            "to": to, "cursor": cursor, "limit": limit}.items() if v is not None}
        r = self._h.get("/api/v1/audit/log", params=params)
        raise_for_status(r); return r.json()

class Client:
    def __init__(self, ...):
        ...
        self.tasks = TasksAPI(self._http)
        self.quota = QuotaAPI(self._http)
        self.executors = ExecutorsAPI(self._http)
        self.audit = AuditAPI(self._http)
    def me(self) -> dict:
        r = self._http.get("/api/v1/auth/me"); raise_for_status(r); return r.json()
```
`TasksAPI` gains:
```python
    def events(self, task_id, *, limit=50, cursor=None) -> dict:
        params = {"limit": limit}
        if cursor is not None: params["cursor"] = cursor
        r = self._h.get(f"/api/v1/tasks/{task_id}/events", params=params)
        raise_for_status(r); return r.json()
```
`aclient.py` (async) mirrors all of the above as `async def` / `await`.

(Note `from_` → query param `from` — `from` is a Python keyword, so the SDK kwarg
is `from_` mapped to the `from` query key. Match the controller's `from`/`to`
param names — verify in `api/audit.py`.)

## 3. CLI extension (`src/dlw/cli/`)

`main.py::_build_parser` — add subparsers:
- `whoami` (no args)
- `quota` (no args)
- `exec` with a sub-subparser `list` (`--status`); or simply `exec-list` if a
  nested subparser is awkward in the existing flat pattern — **prefer `dlw exec list`**
  via a nested subparser, but if the existing `_build_parser` is strictly flat,
  use `dlw exec` (defaulting to list) + `--status`. Implementer picks the form
  that matches the existing argparse structure cleanly.
- `events` (`task_id` positional, `--limit`, `--cursor`, `--follow`)
- `audit` (`--action`, `--actor`, `--from`, `--to`, `--limit`, `--cursor`)

`handlers.py::run` — new `if args.cmd == ...:` branches calling the SDK methods,
emitting via the new `_emit_obj`. For `events --follow`, open the SSE stream and
print each `data:` line's JSON until the stream ends (httpx streaming; reuse the
auth/base_url from the client — add a small `client.tasks.events_stream(task_id)`
context-manager OR have the handler open `client._http.stream("GET", url)`
directly). Keep `--follow` minimal: print each event line, exit on stream close.

`main.py::_emit_obj(obj, args)`:
```python
def _emit_obj(obj, args):
    if args.output == "json":
        print(json.dumps(obj, default=str)); return
    if isinstance(obj, dict) and "items" in obj and isinstance(obj["items"], list):
        _emit_rows(obj["items"], args)        # list-of-dicts → column table
    elif isinstance(obj, list):
        _emit_rows(obj, args)
    else:
        for k, v in obj.items():              # flat dict → key: value lines
            print(f"{k}: {v}")
```
`_emit_rows` computes column widths over the union of keys (or a per-command
field subset) — keep it generic + simple.

## 4. Tests

Extend the established patterns:
- **`tests/sdk/_mock.py`**: add URL handlers for `/api/v1/auth/me`,
  `/api/v1/quota/current`, `/api/v1/executors`, `/api/v1/tasks/{id}/events`,
  `/api/v1/audit/log` returning realistic JSON (matching the real response
  schemas).
- **`tests/cli/test_cli_readonly.py`** (new): drive `cli.main(["whoami"])`,
  `["quota"]`, `["exec","list"]`, `["events","<id>"]`, `["audit"]` via the
  `_wire` mock-transport fixture; assert table + `-o json` output + the right
  endpoint was hit. Cover `--status`/`--action`/`--limit` param passing.
- **`tests/sdk/test_client_async.py`** (extend): `await aclient.me()`,
  `aclient.quota.current()`, `aclient.executors.list()`,
  `aclient.tasks.events(id)`, `aclient.audit.search()` against the real ASGI app
  (the `_bootstrap` seeds tenant/quota/executor; may need to seed an audit row /
  a task with events — reuse existing seeding, add minimal rows).
- **`tests/cli/`**: error-path coverage (e.g. `events <bad-uuid>` → NotFound exit
  3) reusing the existing exit-code test pattern.
- `events --follow`: a focused test with a mock SSE stream (the mock transport
  returns an event-stream body) asserting it prints events and exits on close;
  if SSE-over-MockTransport is awkward, cover `--follow` with the async ASGI
  client against the real `/events/stream` endpoint (bounded via the stream's
  natural close), else mark the streaming test minimal.

## 5. Milestones

- **M1 — SDK**: new resource classes + `Client.me/quota/executors/audit` +
  `TasksAPI.events` + async mirrors + SDK tests (mock + async ASGI) + backend gate.
- **M2 — CLI**: subcommands + `_emit_obj`/`_emit_rows` + handlers + `events
  --follow` SSE + CLI tests + backend gate.
- **M3 — docs**: update `docs/operator/cli-sdk.md` — move `whoami/quota/exec/
  events/audit` from "deferred" to "available"; keep `login`/`materialize`/etc.
  in the deferred list; document `events --follow`. Backend gate.

## 6. Risks & Contingencies

- **Purely additive**: existing `submit/list/show/cancel/delete/watch` + their
  tests are untouched (regression-proof). New SDK namespaces are independent.
- **Mixed SDK return types**: `tasks.*` returns the `DownloadTask` dataclass; the
  new read-only methods return parsed dicts. Acceptable + documented (read-only
  metadata; no value in new model classes). Consistent within the new surface.
- **`from`/`to` audit params**: Python keyword `from` → SDK kwarg `from_`; verify
  the controller's exact query-param names in `api/audit.py` and map precisely.
- **`exec` subcommand form**: nested `dlw exec list` vs flat — implementer matches
  the existing argparse structure; either is fine, document the chosen form.
- **`events --follow` SSE**: httpx streams natively; the endpoint self-terminates
  on terminal status. Keep the follow loop minimal (print + exit on close); if
  the mock-transport SSE test is awkward, use the async ASGI client. No new dep.
- **No openapi change** (these endpoints are already in the spec / are runtime),
  no null examples. No migration. No frontend.
- **CI doesn't gate ruff** — real gate is pytest + `lint_invariants`; `ruff
  --select I001 --fix` new files only.

## 7. Self-Review

- **Closes the high-value deferred slice**: the read-only commands operators most
  want (`quota`, `exec`, `audit`, `events`, `whoami`) all wrap endpoints that
  already accept token auth — zero backend work. ✓
- **Honestly defers** the backend-blocked items (`login` device-flow, config
  write, materialize, retry/upgrade, storage/template/admin) with the reason. ✓
- **Additive / low-risk**: no existing command or test changes; new namespaces +
  subcommands + a generic emitter. ✓
- **Placeholder scan**: the `exec` form choice + the `--follow` test fallback are
  deliberate implementer-judgment points with specified outcomes, not TODOs. ✓
- **Consistency**: SDK resource-class pattern, CLI subparser+handler pattern,
  mock-transport + ASGI test patterns all mirror the merged SP4 CLI/SDK.
