# FU1 — CLI `watch`→SSE + config write/context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade `dlw watch` to consume the self-terminating task SSE stream (`/tasks/{id}/stream`, full `TaskDetail`) and add config write-back commands (`dlw context list/use`, `dlw config set-context/view`).

**Architecture:** New SDK `TasksAPI.task_stream` seam (sync+async) mirroring `events_stream`; `watch` handler streams + prints `TaskDetail` snapshots, exits on terminal. `_config.py` gains `save_config`/`set_context`/`use_context` (reusing the existing `_load_config`); new local-only CLI commands write `~/.dlw/config.yaml`.

**Tech Stack:** Python 3.12, httpx (sync+async streaming), argparse, PyYAML; pytest asyncio_mode=auto, httpx MockTransport + ASGITransport.

**Spec:** `docs/superpowers/specs/2026-05-22-fu1-cli-watch-sse-config-design.md` (read fully — the watch behavior-change mitigations, the build-without-client guard, deferrals).

**Locked constraints (do NOT violate):**
- `watch` keeps its arg signature (`task_id`, `--interval`, `--timeout`) + exit codes (1 on failed, 0 else). `--interval` is accepted but ignored (server drives the tick) — documented. The mechanism changes from polling to SSE; update the existing watch test to the SSE path (preserve its exit-code intent).
- `context`/`config` commands are LOCAL-FILE ONLY — they must NOT build a `Client` (no token required). Branch on these BEFORE `client = make_client(args)` in `handlers.run`.
- Sync httpx CANNOT drive ASGITransport (SP4-CLI lesson): the watch CLI test uses a buffered MockTransport `data:` body; the real-endpoint stream test is async with `max_ticks=1`.
- Config write: plaintext token at rest (matches the existing read path), `chmod 600` best-effort. Reuse `_load_config` for read-merge.
- No backend/openapi/migration/frontend change. CI doesn't gate ruff — real gate pytest + `lint_invariants`; `ruff --select I001 --fix` new files only.
- Additive elsewhere: submit/list/show/cancel/delete + the SP4-CLI readonly commands untouched.

---

## File Structure

- **Modify** `src/dlw/sdk/client.py` + `aclient.py` — `TasksAPI.task_stream` (sync+async).
- **Modify** `src/dlw/sdk/_config.py` — `save_config`/`set_context`/`use_context`/`_resolve_write_path`.
- **Modify** `src/dlw/cli/main.py` — `context`/`config` subparsers; pass-through unchanged for watch.
- **Modify** `src/dlw/cli/handlers.py` — `watch`→SSE; `context`/`config` branches (before make_client).
- **Modify** `tests/sdk/_mock.py` — add `/tasks/{id}/stream` buffered SSE route.
- **Create** `tests/sdk/test_config_write.py`, `tests/cli/test_cli_config.py`; **Modify** `tests/cli/test_cli_ops.py` (watch test → SSE).
- **Modify** `docs/operator/cli-sdk.md`.

---

## Milestone M1 — SDK seam + config write helpers

### Task 1: `task_stream` (sync+async) + `/stream` mock + stream tests

**Files:**
- Modify: `src/dlw/sdk/client.py`, `src/dlw/sdk/aclient.py`, `tests/sdk/_mock.py`
- Test: `tests/sdk/test_readonly.py` (extend) + `tests/sdk/test_client_async.py` (extend)

- [ ] **Step 1: Add the `/tasks/{id}/stream` mock route** in `tests/sdk/_mock.py` (the handler uses `re.fullmatch` for parameterized paths — match `r"/api/v1/tasks/([^/]+)/stream"`). Return a buffered SSE body with ONE terminal TaskDetail snapshot:
```python
    m = re.fullmatch(r"/api/v1/tasks/([^/]+)/stream", path)
    if m and request.method == "GET":
        tid = m.group(1)
        detail = {"id": tid, "repo_id": "o/r", "revision": "a"*40,
                  "status": "succeeded", "priority": 1, "created_at": None,
                  "completed_at": None, "error_message": None, "subtasks": []}
        body = f":open\n\ndata: {json.dumps(detail)}\n\n"
        return httpx.Response(200, text=body,
                              headers={"content-type": "text/event-stream"})
```
(Place it among the GET routes; `json` is imported in `_mock.py` — verify.)

- [ ] **Step 2: Write the failing sync stream test.** In `tests/sdk/test_readonly.py`, add:
```python
def test_task_stream_buffered(sync_client):
    lines = []
    with sync_client.tasks.task_stream("33333333-3333-3333-3333-333333333333") as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            lines.append(line)
    data = [l for l in lines if l.startswith("data: ")]
    assert data and '"status": "succeeded"' in data[0]
```

- [ ] **Step 3: Verify FAIL** (`task_stream` missing).

- [ ] **Step 4: Implement `task_stream`.** In `src/dlw/sdk/client.py` `TasksAPI` (next to `events_stream`):
```python
    def task_stream(self, task_id: str, *, max_ticks: int | None = None):
        """SSE seam for `dlw watch`: streams TaskDetail from
        /tasks/{id}/stream (self-terminates on terminal status)."""
        params = {"max_ticks": max_ticks} if max_ticks is not None else None
        return self._h.stream(
            "GET", f"/api/v1/tasks/{task_id}/stream", params=params)
```
And the async mirror in `aclient.py` `AsyncTasksAPI` (same body — `self._h.stream(...)`; async httpx stream CM).

- [ ] **Step 5: Add the async real-endpoint test** in `tests/sdk/test_client_async.py` (mirror the `events_stream` async test added in SP4-CLI; submit a task to get a real id, then):
```python
async def test_task_stream_async(aclient):
    t = await aclient.tasks.submit(repo_id="o/r", revision="b"*40, storage_id=1)
    lines = []
    async with aclient.tasks.task_stream(t.id, max_ticks=1) as r:
        async for line in r.aiter_lines():
            lines.append(line)
    assert any(l.startswith("data: ") or l.startswith(":open") for l in lines)
```
(Reuse the submit flow from the existing async tests; `max_ticks=1` bounds it.)

- [ ] **Step 6: Verify PASS** (`cd "D:/download_weights" && uv run pytest tests/sdk/test_readonly.py tests/sdk/test_client_async.py -k "stream or task_stream" -v`) → pass.

- [ ] **Step 7: Tidy + commit.**
```bash
cd "D:/download_weights" && uv run ruff check --select I001 --fix src/dlw/sdk/client.py src/dlw/sdk/aclient.py tests/sdk/_mock.py tests/sdk/test_readonly.py tests/sdk/test_client_async.py
git add src/dlw/sdk/client.py src/dlw/sdk/aclient.py tests/sdk/_mock.py tests/sdk/test_readonly.py tests/sdk/test_client_async.py && git commit -m "feat(fu1): TasksAPI.task_stream SSE seam (sync+async)"
```

### Task 2: `_config.py` write helpers

**Files:**
- Modify: `src/dlw/sdk/_config.py`
- Test: `tests/sdk/test_config_write.py`

- [ ] **Step 1: Write the failing tests.** Create `tests/sdk/test_config_write.py`:
```python
"""FU1 config write-back round-trip."""
from __future__ import annotations

import pytest

from dlw.sdk._config import resolve, save_config, set_context, use_context
from dlw.sdk.errors import UsageError


def test_set_context_roundtrip(tmp_path):
    p = str(tmp_path / "config.yaml")
    set_context("dev", server="http://h:8000/", token="T1", config_path=p)
    r = resolve(server=None, token=None, config_path=p)
    assert r.server == "http://h:8000" and r.token == "T1"


def test_use_context_switches(tmp_path):
    p = str(tmp_path / "config.yaml")
    set_context("a", server="http://a", token="TA", config_path=p)
    set_context("b", server="http://b", token="TB", make_current=False,
                config_path=p)
    assert resolve(server=None, token=None, config_path=p).server == "http://a"
    use_context("b", config_path=p)
    assert resolve(server=None, token=None, config_path=p).server == "http://b"


def test_use_missing_context_errors(tmp_path):
    p = str(tmp_path / "config.yaml")
    set_context("a", server="http://a", token="TA", config_path=p)
    with pytest.raises(UsageError):
        use_context("nope", config_path=p)


def test_save_creates_parent_dir(tmp_path):
    p = str(tmp_path / "nested" / "dir" / "config.yaml")
    save_config({"current_context": "x"}, config_path=p)
    import os
    assert os.path.isfile(p)
```

- [ ] **Step 2: Verify FAIL** (helpers missing).

- [ ] **Step 3: Implement** in `src/dlw/sdk/_config.py` (add `from pathlib import Path` is already imported; add the helpers per spec §2): `_resolve_write_path`, `save_config`, `set_context`, `use_context`. (Copy the spec's code verbatim. `_load_config` already exists — reuse for the read-merge in set/use.)

- [ ] **Step 4: Verify PASS** + config regression (`cd "D:/download_weights" && uv run pytest tests/sdk/test_config_write.py tests/sdk/test_config.py -v`) → all pass.

- [ ] **Step 5: Tidy + commit.**
```bash
cd "D:/download_weights" && uv run ruff check --select I001 --fix src/dlw/sdk/_config.py tests/sdk/test_config_write.py
git add src/dlw/sdk/_config.py tests/sdk/test_config_write.py && git commit -m "feat(fu1): config write-back (save_config/set_context/use_context)"
```

### Task 3: M1 backend gate

- [ ] `cd "D:/download_weights" && uv run pytest -q` → all pass (the failover flake is Windows-local; re-run isolated to confirm if it appears). `python tools/lint_invariants.py --strict` → OK. No commit.

---

## Milestone M2 — CLI

### Task 4: `watch`→SSE + `context`/`config` commands

**Files:**
- Modify: `src/dlw/cli/main.py`, `src/dlw/cli/handlers.py`
- Test: `tests/cli/test_cli_config.py` (new), `tests/cli/test_cli_ops.py` (watch test → SSE)

- [ ] **Step 1: Update the existing watch test + write the failing config tests.**
  - In `tests/cli/test_cli_ops.py`, rewrite `test_watch_terminal_exit0` to the SSE path: the `_mock.py` `/tasks/{id}/stream` route now returns a terminal (`succeeded`) snapshot, so `cli.main(["watch", tid])` should consume the stream and exit 0 (no `TasksAPI.get` monkeypatch needed). If a `failed`-status variant is wanted, parametrize the mock or add a second test asserting exit 1 (the default mock returns `succeeded` → exit 0; for failed, you may submit with a repo id the mock maps to failed, OR keep just the exit-0 case + a unit test of the failed path in the handler). Keep it simple: assert `cli.main(["watch", tid]) == 0`.
  - Create `tests/cli/test_cli_config.py` (uses a tmp config path; NO `_transport`/token needed — these are local-file commands):
```python
"""FU1 config/context CLI commands (local file, no network)."""
from __future__ import annotations

from dlw.cli import main as cli


def test_set_context_and_list(capsys, tmp_path):
    p = str(tmp_path / "c.yaml")
    assert cli.main(["-c", p, "config", "set-context", "dev",
                     "--server", "http://h:8000", "--token", "TK"]) == 0
    assert cli.main(["-c", p, "context", "list"]) == 0
    out = capsys.readouterr().out
    assert "dev" in out and "current" in out.lower()


def test_config_view_redacts_token(capsys, tmp_path):
    p = str(tmp_path / "c.yaml")
    cli.main(["-c", p, "config", "set-context", "dev",
              "--server", "http://h", "--token", "SECRET"])
    capsys.readouterr()
    assert cli.main(["-c", p, "config", "view"]) == 0
    out = capsys.readouterr().out
    assert "SECRET" not in out and "set" in out.lower()


def test_context_use_switches(capsys, tmp_path):
    p = str(tmp_path / "c.yaml")
    cli.main(["-c", p, "config", "set-context", "a", "--server", "http://a",
              "--token", "TA"])
    cli.main(["-c", p, "config", "set-context", "b", "--server", "http://b",
              "--token", "TB"])
    assert cli.main(["-c", p, "context", "use", "a"]) == 0
```

- [ ] **Step 2: Verify FAIL** (`cd "D:/download_weights" && uv run pytest tests/cli/test_cli_config.py -v`) → unknown commands → exit 2.

- [ ] **Step 3: Add subparsers** in `main.py::_build_parser` (after the readonly commands, before `return p`):
```python
    ctx = sub.add_parser("context", help="manage CLI contexts")
    ctx_sub = ctx.add_subparsers(dest="context_cmd")
    ctx_sub.add_parser("list", help="list contexts")
    ctx_use = ctx_sub.add_parser("use", help="switch current context")
    ctx_use.add_argument("name")

    cfg = sub.add_parser("config", help="manage CLI config file")
    cfg_sub = cfg.add_subparsers(dest="config_cmd")
    cfg_set = cfg_sub.add_parser("set-context", help="create/update a context")
    cfg_set.add_argument("name")
    cfg_set.add_argument("--server", default=None)
    cfg_set.add_argument("--token", default=None)
    cfg_set.add_argument("--no-current", action="store_true",
                         help="do not switch current_context to this one")
    cfg_sub.add_parser("view", help="show config path + current context")
```

- [ ] **Step 4: Implement handlers** in `handlers.py`. FIRST, guard local-file commands before `make_client` (they need no token). At the top of `run`, before `client = make_client(args)`:
```python
    if args.cmd in ("context", "config"):
        return _config_cmd(args)
```
Then add `_config_cmd(args)` + the SSE watch helper:
```python
def _config_cmd(args) -> int:
    import sys
    from dlw.sdk import _config as cfgmod
    cfgpath = args.config
    if args.cmd == "config" and args.config_cmd == "set-context":
        p = cfgmod.set_context(args.name, server=args.server, token=args.token,
                               make_current=not args.no_current,
                               config_path=cfgpath)
        if not args.quiet:
            sys.stdout.write(f"wrote context '{args.name}' to {p}\n")
        return 0
    if args.cmd == "config" and args.config_cmd == "view":
        cfg = cfgmod._load_config(cfgpath)
        cur = cfg.get("current_context")
        sys.stdout.write(f"current_context: {cur}\n")
        for name, c in (cfg.get("contexts") or {}).items():
            tok = ((cfg.get("auth") or {}).get(name) or {}).get("access_token")
            mark = " (current)" if name == cur else ""
            sys.stdout.write(f"  {name}{mark}: server={c.get('server')} "
                             f"token={'set' if tok else 'unset'}\n")
        return 0
    if args.cmd == "context" and args.context_cmd == "list":
        cfg = cfgmod._load_config(cfgpath)
        cur = cfg.get("current_context")
        ctxs = cfg.get("contexts") or {}
        if not ctxs:
            sys.stdout.write("(no contexts)\n")
        for name in ctxs:
            sys.stdout.write(f"{name}{' (current)' if name == cur else ''}\n")
        return 0
    if args.cmd == "context" and args.context_cmd == "use":
        from dlw.sdk.errors import exit_code_for, DlwError
        try:
            cfgmod.use_context(args.name, config_path=cfgpath)
        except DlwError as exc:
            sys.stderr.write(f"Error: {exc.message}\n")
            return exit_code_for(exc)
        if not args.quiet:
            sys.stdout.write(f"switched to context '{args.name}'\n")
        return 0
    sys.stderr.write("usage: dlw context [list|use NAME] | "
                     "dlw config [set-context NAME ...|view]\n")
    return 2
```
(NOTE: `use_context` raises `UsageError` which is a `DlwError`; the outer `main()` already maps `DlwError`→exit code, but since `_config_cmd` returns before the `make_client` try, catch it here OR let it propagate — simplest: let it propagate to `main()`'s `except DlwError`. Actually `_config_cmd` is called inside `run()`'s body which is inside `main()`'s `try` — so a raised `UsageError` propagates to `main()`'s handler → correct exit code. So you can DROP the try/except in the `use` branch and just call `cfgmod.use_context(...)`; the `main()` handler maps it. Verify `run` doesn't swallow it — `run`'s try/finally only does `client.close()` in finally; but `client` isn't built for config cmds. Ensure the early `return _config_cmd(args)` is BEFORE the `client = make_client(args)` + its try/finally, so no client cleanup runs. Keep `_config_cmd` simple — let `UsageError` propagate.)

- [ ] **Step 5: Replace the `watch` handler branch** with SSE consumption:
```python
        if args.cmd == "watch":
            return _watch_sse(client, args, emit)
```
And add the helper (module level in handlers.py):
```python
def _watch_sse(client, args, emit) -> int:
    import json
    import time
    deadline = (time.monotonic() + args.timeout) if args.timeout else None
    last = None
    terminal = {"succeeded", "failed", "cancelled"}
    with client.tasks.task_stream(args.task_id) as r:
        if r.status_code != 200:
            r.read()
            from dlw.sdk._http import raise_for_status
            raise_for_status(r)
        for line in r.iter_lines():
            if deadline and time.monotonic() > deadline:
                from dlw.sdk.errors import Timeout
                raise Timeout("watch timed out")
            if not line.startswith("data: "):
                continue
            detail = json.loads(line[len("data: "):])
            last = detail
            subs = detail.get("subtasks") or []
            done = sum(1 for s in subs if s.get("status") == "succeeded")
            sys.stdout.write(f"{detail.get('status')} {done}/{len(subs)}\n")
            sys.stdout.flush()
            if detail.get("status") in terminal:
                break
    if last is None or last.get("status") not in terminal:
        last = _task_dict(client.tasks.get(args.task_id))   # safety net
    emit(last, args)
    return 1 if last.get("status") == "failed" else 0
```
(`Timeout` is in `dlw.sdk.errors` — verify the name; if it's `TimeoutError`-aliased differently, match it. The safety-net `_task_dict(client.tasks.get(...))` returns a dict shaped like the emit expects. `emit` here is the `_emit` passed into `run`.)

- [ ] **Step 6: Verify PASS + full CLI regression:** `cd "D:/download_weights" && uv run pytest tests/cli/ -v` → all pass (the rewritten watch test + new config tests + the untouched submit/list/cancel/delete/readonly tests).

- [ ] **Step 7: Tidy + commit.**
```bash
cd "D:/download_weights" && uv run ruff check --select I001 --fix src/dlw/cli/main.py src/dlw/cli/handlers.py tests/cli/test_cli_config.py tests/cli/test_cli_ops.py
git add src/dlw/cli/main.py src/dlw/cli/handlers.py tests/cli/test_cli_config.py tests/cli/test_cli_ops.py && git commit -m "feat(fu1): watch->SSE + dlw context/config commands"
```

### Task 5: M2 backend gate

- [ ] `cd "D:/download_weights" && uv run pytest -q` → all pass (failover flake = Windows-local; isolate-confirm if it appears). `python tools/lint_invariants.py --strict` → OK. No commit.

---

## Milestone M3 — docs

### Task 6: Update operator CLI docs

**Files:**
- Modify: `docs/operator/cli-sdk.md`

- [ ] **Step 1:** Document: `dlw watch` now STREAMS the task SSE (richer per-snapshot progress, self-terminating on terminal; `--interval` accepted-but-ignored, `--timeout` still bounds); the new `dlw context list/use` + `dlw config set-context/view` commands + the `~/.dlw/config.yaml` schema (`current_context`, `contexts.<name>.server`, `auth.<name>.access_token`, written with mode 600, plaintext token). Update §5/§6: move "polling watch" out of limitations (now streams); keep `login` device-flow + token-at-rest-encryption + config beyond-basic deferred. Note `config view` redacts tokens.

- [ ] **Step 2: Commit.**
```bash
cd "D:/download_weights" && git add docs/operator/cli-sdk.md && git commit -m "docs(fu1): watch streams; document context/config commands"
```

---

## Self-Review

**1. Spec coverage:** §1.1 task_stream → Task 1 ✓; §1.2 watch→SSE → Task 4 ✓; §1.3 config write → Tasks 2,4 ✓; §2 code → Tasks 1,2,4 ✓; §3 CLI → Task 4 ✓; §4 tests → Tasks 1,2,4 ✓; §5 milestones → M1/M2/M3 ✓.

**2. Placeholder scan:** Task 4 Step 1 (watch test simplification to exit-0) + Step 4 (let UsageError propagate) are decided behaviors with concrete outcomes, not TODOs. The `Timeout` error-name + `_task_dict` reuse are implementer-verify points with specified intent.

**3. Type consistency:** `task_stream(task_id, *, max_ticks=None)` (sync+async); `save_config(cfg, *, config_path=None)→Path`; `set_context(name, *, server, token, make_current, config_path)`; `use_context(name, *, config_path)`; `_config_cmd(args)→int`; `_watch_sse(client, args, emit)→int`. Consistent.

**Open risks for reviewers:** (a) does `dlw.sdk.errors` export `Timeout` (the watch deadline raises it) — name check? (b) the watch test rewrite — does the `_mock.py` `/stream` route's terminal snapshot make `cli.main(["watch", tid])` exit 0 without the old `TasksAPI.get` monkeypatch (confirm the old test's monkeypatch is removed)? (c) `_config_cmd` returns before `make_client` — confirm `run`'s structure lets the early return skip client construction + the finally-close; (d) `config view`/`context list` call `_config._load_config` (private) — acceptable, or add a public accessor? (e) does `args.config` (global `-c`) reach the config subcommands (it's a top-level arg, so yes — confirm it's on the namespace).
