# FU1 — CLI `watch`→SSE + config write/context (Design)

> First of three named follow-ons the user queued ("顺序执行 1-4"). Two CLI/SDK
> improvements: (a) upgrade `dlw watch` to consume the task SSE stream
> (`GET /tasks/{id}/stream`, which self-terminates on terminal status and
> carries full `TaskDetail`) instead of polling; (b) add config write-back —
> `dlw context list/use` + `dlw config set-context` + `dlw config view` — so a
> user can persist server/token contexts to `~/.dlw/config.yaml` (the read path
> already exists in `_config.py`).
> Status: self-approved per Rule #1. Branch: `feat/fu1-cli-watch-sse-config`.

## 1. Scope

**In scope (additive + one command upgrade; no backend change, no migration, no new dep):**

1. **SDK `TasksAPI.task_stream(task_id, *, max_ticks=None)`** (sync + async) — a
   streaming context manager over `GET /api/v1/tasks/{id}/stream` (mirrors the
   `events_stream` seam from SP4-CLI). Caller iterates `data:` lines; the
   endpoint self-terminates on terminal status (verified `tasks_stream.py:93`).
2. **`dlw watch` upgraded to SSE.** Consume `task_stream`, parse each
   `data: <TaskDetail JSON>` snapshot, print a progress line per snapshot
   (`status files_done/total`), and stop when a snapshot's status is terminal
   (the stream closes itself). Exit `1` on `failed`, else `0`. Flags kept for
   compat: `--timeout` wraps a client-side deadline; `--interval` is **accepted
   but ignored** (the server drives the 1 Hz tick) — documented. Safety net: if
   the stream closes without a terminal snapshot (rare mid-stream disconnect),
   do ONE final `tasks.get` to resolve the terminal state. A non-200 stream open
   surfaces the mapped error (404→NotFound→exit 3, etc.).
3. **Config write-back** (`src/dlw/sdk/_config.py` + new CLI commands):
   - `save_config(cfg: dict, *, config_path=None) -> Path` — write YAML to the
     resolved path (`--config`/`DLW_CONFIG` > `$XDG_CONFIG_HOME/dlw/config.yaml`
     > `~/.dlw/config.yaml`), creating the parent dir.
   - `set_context(name, *, server=None, token=None, make_current=True,
     config_path=None)` — merge `contexts.<name>.server` +
     `auth.<name>.access_token`, optionally set `current_context`; load-merge-save.
   - `use_context(name, *, config_path=None)` — set `current_context` (error if
     the context doesn't exist).
   - CLI: `dlw context list` (show contexts, mark current), `dlw context use
     <name>`, `dlw config set-context <name> [--server URL] [--token JWT]
     [--no-current]`, `dlw config view` (show the resolved config path + current
     context + a redacted summary — never print full tokens).

**Out of scope (named, deferred):**
- `dlw login`/`logout` — OIDC device-code flow endpoint (`POST /auth/device`)
  still doesn't exist (FU1 only persists a token the user already has).
- `watch` polling fallback as a user-selectable mode — SSE is now the path; the
  one-shot final `tasks.get` is the only residual poll (resolution safety net).
- Token encryption at rest in the config file — the existing read path stores
  plaintext `access_token`; FU1 matches that (documented; chmod 600 on write).

## 2. SDK: `task_stream` + config write

`client.py` `TasksAPI` (and `aclient.py` `AsyncTasksAPI`):
```python
    def task_stream(self, task_id, *, max_ticks=None):
        """SSE seam for `dlw watch`. Streaming context manager over
        /tasks/{id}/stream (self-terminates on terminal status)."""
        params = {"max_ticks": max_ticks} if max_ticks is not None else None
        return self._h.stream(
            "GET", f"/api/v1/tasks/{task_id}/stream", params=params)
```
(async mirror returns the async stream CM, used `async with ... as r: async for line in r.aiter_lines()`.)

`_config.py` additions (write-back; reuse the existing path-resolution logic):
```python
def _resolve_write_path(config_path: str | None) -> Path:
    if config_path:
        return Path(config_path)
    env = os.environ.get("DLW_CONFIG")
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "dlw" / "config.yaml"
    return Path.home() / ".dlw" / "config.yaml"

def save_config(cfg: dict, *, config_path: str | None = None) -> Path:
    p = _resolve_write_path(config_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(cfg, sort_keys=True), encoding="utf-8")
    try:
        p.chmod(0o600)          # best-effort; tokens live here
    except OSError:
        pass
    return p

def set_context(name, *, server=None, token=None, make_current=True,
                config_path=None) -> Path:
    cfg = _load_config(config_path) or {}
    cfg.setdefault("contexts", {}).setdefault(name, {})
    if server is not None:
        cfg["contexts"][name]["server"] = server.rstrip("/")
    if token is not None:
        cfg.setdefault("auth", {}).setdefault(name, {})["access_token"] = token
    if make_current:
        cfg["current_context"] = name
    return save_config(cfg, config_path=config_path)

def use_context(name, *, config_path=None) -> Path:
    cfg = _load_config(config_path) or {}
    if name not in (cfg.get("contexts") or {}):
        raise UsageError(f"no such context: {name}")
    cfg["current_context"] = name
    return save_config(cfg, config_path=config_path)
```
(`_load_config` already exists; reuse it for the read-merge step. Note `_load_config("")` returns `{}` — the write helpers use the same resolution but for a path, so an explicit `--config <file>` writes there.)

## 3. CLI

`main.py::_build_parser` — new subparsers:
- `context` → nested `list` (no args) + `use` (`name`).
- `config` → nested `set-context` (`name`, `--server`, `--token`, `--no-current`)
  + `view` (no args).
- `watch` keeps its existing args (`task_id`, `--interval`, `--timeout`) — no
  signature change; only the handler body changes to SSE.

`handlers.py`:
- `watch` branch → `_watch_sse(client, args, emit)`: open `client.tasks.task_stream(
  args.task_id)`, iterate `data:` lines, JSON-parse each TaskDetail, print a
  progress line (reuse a small `_progress(detail_dict)` → `status N/total`), track
  the last status; on a terminal status break + `emit` the final dict + return
  `1 if failed else 0`. Wrap with a `--timeout` deadline (time.monotonic check
  between reads; on timeout exit 9). On non-terminal stream close, do one
  `client.tasks.get` + emit. Non-200 open → `raise_for_status` (mapped exit).
- `context`/`config` branches → call the `_config.py` write helpers; `config
  view` prints the resolved path + `current_context` + per-context server +
  `token: <set|unset>` (NEVER the token value).

Config commands resolve the write path from `args.config` (the global `-c`),
matching the read precedence. They do NOT need a `Client`/token (pure local
file ops) — so the handler must build them WITHOUT `make_client` (which would
require a token). Branch on `args.cmd in {"context","config"}` BEFORE
`client = make_client(args)` (or guard `make_client` for these).

## 4. Tests

- **`tests/sdk/test_config_write.py`** (new): `set_context`/`use_context`/
  `save_config` against a `tmp_path` config file (pass `config_path=`); assert
  YAML round-trips, `current_context` set, `use_context` on a missing context →
  `UsageError`, file mode best-effort. Then `resolve(config_path=...)` reads back
  the written server/token (the read+write round-trip).
- **`tests/cli/test_cli_config.py`** (new): `dlw -c <tmp> config set-context dev
  --server http://h --token T` then `dlw -c <tmp> context list` shows `dev
  (current)`; `dlw -c <tmp> config view` shows the path + `token: set` (NOT the
  value); `context use` switches. Uses `cli.main([...])` + a tmp config path (no
  network/token needed for these).
- **`tests/cli/test_cli_watch_sse.py`** or extend the watch test: drive `dlw
  watch <id>` where the SDK's `task_stream` is exercised. Sync path: a mock
  transport returning a buffered `data: {TaskDetail terminal}\n\n` body → assert
  the final emit + exit code (0 succeeded / 1 failed). Real-stream path (async):
  `AsyncTasksAPI.task_stream(real_task_id, max_ticks=1)` over ASGITransport (sync
  httpx can't drive ASGI — SP4-CLI lesson) asserting a `data:` snapshot arrives.
  The watch CLI test uses the buffered mock (deterministic).
- Backward-compat: existing `tests/cli/test_cli_ops.py::watch` test — the SSE
  upgrade changes watch's mechanism; if that test asserted polling specifics,
  update it to the SSE behavior (it likely just checks exit code + final output,
  which the SSE path preserves). Keep its exit-code assertions green.

## 5. Milestones

- **M1 — SDK + config helpers**: `task_stream` (sync+async) + `_config.py`
  write helpers + `test_config_write.py` + sync/async stream tests + backend gate.
- **M2 — CLI**: `watch`→SSE handler + `context`/`config` commands + the
  build-without-client guard + CLI tests + backend gate.
- **M3 — docs**: update `docs/operator/cli-sdk.md` — `watch` now streams;
  document `context`/`config` commands + the config file schema; move "polling
  watch" out of the deferred/limitations note; keep `login` deferred.

## 6. Risks & Contingencies

- **`watch` is a working command — SSE upgrade is the one behavior change.**
  Mitigations: keep flags + exit codes; final-`get` safety net on non-terminal
  close; non-200 open surfaces the mapped error; the SSE endpoint is well-tested
  (UI-SP5). `--interval` kept-but-ignored (documented) to avoid breaking scripts.
- **Sync httpx can't drive ASGITransport** (SP4-CLI lesson): the watch CLI test
  uses a buffered MockTransport `data:` body; the real-endpoint stream test is
  async with `max_ticks=1`.
- **Config write is local-file only** — no token needed; the handler must NOT
  call `make_client` for `context`/`config` (would demand a token). Plaintext
  token at rest matches the existing read path; `chmod 600` best-effort.
- **No backend/openapi/migration/frontend change.** CI doesn't gate ruff — real
  gate pytest + lint_invariants; `ruff --select I001 --fix` new files only.

## 7. Self-Review

- **watch→SSE**: richer (TaskDetail snapshots), self-terminating, exit codes
  preserved, safety net on disconnect. ✓
- **config write**: additive commands + helpers; round-trips with the existing
  read path; no token needed; tokens redacted in `view`. ✓
- **Honest deferrals**: `login` device-flow, token-at-rest encryption — named. ✓
- **Placeholder scan**: the `--interval`-ignored + final-get safety net are
  deliberate documented behaviors, not TODOs.
- **Consistency**: `task_stream` mirrors `events_stream`; config helpers reuse
  `_load_config`; CLI subparser/handler patterns match SP4-CLI.
