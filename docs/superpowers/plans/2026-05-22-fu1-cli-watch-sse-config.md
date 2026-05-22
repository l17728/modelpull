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
- **Create** `tests/sdk/test_config_write.py`, `tests/cli/test_cli_context.py`; **Modify** `tests/cli/test_cli_ops.py` (watch test → SSE).
- **Modify** `docs/operator/cli-sdk.md`.

---

## Milestone M1 — SDK seam + config write helpers

### Task 1: `task_stream` (sync+async) + `/stream` mock + stream tests

**Files:**
- Modify: `src/dlw/sdk/client.py`, `src/dlw/sdk/aclient.py`, `tests/sdk/_mock.py`
- Test: `tests/sdk/test_readonly.py` (extend) + `tests/sdk/test_client_async.py` (extend)

- [ ] **Step 1: Add the `/tasks/{id}/stream` mock route** in `tests/sdk/_mock.py`. **Placement (pre-review B1): insert it AFTER the auth gate (`_mock.py:30`, which returns 401 unless `Bearer good`) and BEFORE the catch-all `404` return — among the other GET routes.** Otherwise it's either bypassed by the auth gate or unreachable past the 404. The handler uses `re.fullmatch` for parameterized paths — match `r"/api/v1/tasks/([^/]+)/stream"`. Return a buffered SSE body with ONE terminal TaskDetail snapshot. To also support a `failed`-exit-1 test, key the status off the task-id (e.g. an id containing `"fail"` → status `failed`):
```python
    m = re.fullmatch(r"/api/v1/tasks/([^/]+)/stream", path)
    if m and request.method == "GET":
        tid = m.group(1)
        status = "failed" if "fail" in tid else "succeeded"
        detail = {"id": tid, "repo_id": "o/r", "revision": "a"*40,
                  "status": status, "priority": 1, "created_at": None,
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
    def task_stream(self, task_id: str, *, max_ticks: int | None = None,
                    timeout=None):
        """SSE seam for `dlw watch`: streams TaskDetail from
        /tasks/{id}/stream (self-terminates on terminal status). `timeout`
        (httpx read-timeout) makes a STALLED stream raise rather than hang."""
        params = {"max_ticks": max_ticks} if max_ticks is not None else None
        kw = {"params": params}
        if timeout is not None:
            kw["timeout"] = timeout
        return self._h.stream(
            "GET", f"/api/v1/tasks/{task_id}/stream", **kw)
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

- [ ] **Step 3: Implement** in `src/dlw/sdk/_config.py` (`Path` already imported; add the helpers per spec §2): `_resolve_write_path`, `save_config`, `set_context`, `use_context`, AND a public `def load_config(config_path=None): return _load_config(config_path)` alias (so the CLI reads via a public name, not the private `_load_config` — pre-review). (Copy the spec's code verbatim. `_load_config` already exists — reuse for the read-merge in set/use.)

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
- Test: `tests/cli/test_cli_context.py` (new), `tests/cli/test_cli_ops.py` (watch test → SSE)

- [ ] **Step 1: Update the existing watch test + write the failing context tests.**
  - In `tests/cli/test_cli_ops.py`, rewrite `test_watch_terminal_exit0` to the SSE path: REMOVE the `TasksAPI.get` monkeypatch (no longer used — watch streams). The `_mock.py` `/tasks/{id}/stream` route returns a terminal `succeeded` snapshot, so `cli.main(["watch", tid]) == 0`. ALSO add `test_watch_failed_exit1`: submit/use a task id containing `"fail"` (so the mock returns status `failed`) and assert `cli.main(["watch", <id-with-fail>]) == 1` — covers the exit-1 contract (pre-review N4). (If `_submit` returns a server-generated uuid you can't control, instead add a unit test calling the handler/`_watch_sse` with a fake client whose `task_stream` yields a `failed` snapshot; OR add a mock route variant. Pick the cleanest given `_submit`'s id source — the simplest is a direct `task_stream` mock returning failed.)
  - Create `tests/cli/test_cli_context.py` (tmp config path; NO `_transport`/token — local-file commands):
```python
"""FU1 context CLI commands (local file, no network)."""
from __future__ import annotations

from dlw.cli import main as cli


def test_set_and_list(capsys, tmp_path):
    p = str(tmp_path / "c.yaml")
    assert cli.main(["-c", p, "context", "set", "dev",
                     "--server", "http://h:8000", "--token", "TK"]) == 0
    assert cli.main(["-c", p, "context", "list"]) == 0
    out = capsys.readouterr().out
    assert "dev" in out and "current" in out.lower()


def test_current_redacts_token(capsys, tmp_path):
    p = str(tmp_path / "c.yaml")
    cli.main(["-c", p, "context", "set", "dev",
              "--server", "http://h", "--token", "SECRET"])
    capsys.readouterr()
    assert cli.main(["-c", p, "context", "current"]) == 0
    out = capsys.readouterr().out
    assert "SECRET" not in out and "set" in out.lower()


def test_use_switches(capsys, tmp_path):
    p = str(tmp_path / "c.yaml")
    cli.main(["-c", p, "context", "set", "a", "--server", "http://a",
              "--token", "TA"])
    cli.main(["-c", p, "context", "set", "b", "--server", "http://b",
              "--token", "TB"])
    assert cli.main(["-c", p, "context", "use", "a"]) == 0


def test_use_missing_exit2(capsys, tmp_path):
    p = str(tmp_path / "c.yaml")
    cli.main(["-c", p, "context", "set", "a", "--server", "http://a",
              "--token", "TA"])
    assert cli.main(["-c", p, "context", "use", "nope"]) == 2   # UsageError
```

- [ ] **Step 2: Verify FAIL** (`cd "D:/download_weights" && uv run pytest tests/cli/test_cli_context.py -v`) → unknown commands → exit 2.

- [ ] **Step 3: Add subparsers** in `main.py::_build_parser` (after the readonly commands, before `return p`). ALL context management under the `context` noun (no separate `config` namespace — pre-review). ALSO update the `watch` parser help: change `help="poll a task until terminal"` → `help="stream a task until terminal"`, and the `--interval` help → `"(deprecated, ignored — server drives the 1 Hz stream tick)"`.
```python
    ctx = sub.add_parser("context", help="manage CLI contexts")
    ctx_sub = ctx.add_subparsers(dest="context_cmd")
    ctx_sub.add_parser("list", help="list contexts (marks current)")
    ctx_sub.add_parser("current", help="show the current context")
    ctx_use = ctx_sub.add_parser("use", help="switch current context")
    ctx_use.add_argument("name")
    ctx_set = ctx_sub.add_parser("set", help="create/update a context")
    ctx_set.add_argument("name")
    ctx_set.add_argument("--server", default=None)
    ctx_set.add_argument("--token", default=None)
    ctx_set.add_argument("--no-current", action="store_true",
                         help="do not switch current_context to this one")
```

- [ ] **Step 4: Implement handlers** in `handlers.py`. FIRST, guard the local-file `context` commands before `make_client` (they need no token). At the top of `run`, BEFORE `client = make_client(args)` (and before the try/finally that closes the client):
```python
    if args.cmd == "context":
        return _context_cmd(args)
```
Then add `_context_cmd(args)` (context-only; `use_context`'s `UsageError` propagates to `main()`'s `except DlwError` handler → exit 2 — no local try/except, per pre-review):
```python
def _context_cmd(args) -> int:
    import sys
    from dlw.sdk import _config as cfgmod
    cfgpath = args.config
    sub = getattr(args, "context_cmd", None)
    if sub == "set":
        p = cfgmod.set_context(args.name, server=args.server, token=args.token,
                               make_current=not args.no_current,
                               config_path=cfgpath)
        if not args.quiet:
            sys.stdout.write(f"wrote context '{args.name}' to {p}\n")
        return 0
    if sub == "use":
        cfgmod.use_context(args.name, config_path=cfgpath)   # UsageError→main()→exit 2
        if not args.quiet:
            sys.stdout.write(f"switched to context '{args.name}'\n")
        return 0
    cfg = cfgmod.load_config(cfgpath)
    cur = cfg.get("current_context")
    if sub == "list":
        sys.stdout.write(f"# config: {cfgmod._resolve_write_path(cfgpath)}\n")
        ctxs = cfg.get("contexts") or {}
        if not ctxs:
            sys.stdout.write("(no contexts)\n")
        for name, c in ctxs.items():
            tok = ((cfg.get("auth") or {}).get(name) or {}).get("access_token")
            mark = " (current)" if name == cur else ""
            sys.stdout.write(f"{name}{mark}: server={c.get('server')} "
                             f"token={'set' if tok else 'unset'}\n")
        return 0
    if sub == "current":
        if not cur:
            sys.stdout.write("(no current context)\n")
            return 0
        c = (cfg.get("contexts") or {}).get(cur) or {}
        tok = ((cfg.get("auth") or {}).get(cur) or {}).get("access_token")
        sys.stdout.write(f"{cur}: server={c.get('server')} "
                         f"token={'set' if tok else 'unset'}\n")
        return 0
    sys.stderr.write("usage: dlw context [list|current|use NAME|set NAME ...]\n")
    return 2
```
(`load_config` is the public alias added in Task 2. `_resolve_write_path` shows the resolved file path. Token value is NEVER printed — only `set|unset`.)

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
    import httpx
    from dlw.sdk._http import raise_for_status
    from dlw.sdk.errors import Timeout
    # --interval is server-driven now: warn (don't silently ignore) if set.
    if getattr(args, "interval", 5.0) != 5.0:
        sys.stderr.write(
            "warning: --interval is deprecated and ignored "
            "(the server drives the 1 Hz stream tick)\n")
    deadline = (time.monotonic() + args.timeout) if args.timeout else None
    last = None
    terminal = {"succeeded", "failed", "cancelled"}
    try:
        with client.tasks.task_stream(args.task_id, timeout=args.timeout) as r:
            if r.status_code != 200:
                r.read()
                raise_for_status(r)        # 404→NotFound→exit 3, etc.
            for line in r.iter_lines():
                if deadline and time.monotonic() > deadline:
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
    except httpx.TimeoutException as e:        # stalled stream → exit 9
        raise Timeout("watch stream timed out") from e
    # Normalize the emit shape: both paths emit a raw TaskDetail-shaped dict.
    if last is None or last.get("status") not in terminal:
        last = client.tasks.get(args.task_id).raw   # safety net (same shape)
    emit(last, args)
    return 1 if last.get("status") == "failed" else 0
```
(`Timeout` confirmed in `dlw.sdk.errors` (exit 9). `DownloadTask.raw` is the raw API dict — same shape as the SSE `data:` TaskDetail dict, so `emit` (the `_emit` passed to `run`) renders identical columns on both paths. `args.timeout` is passed BOTH as the client deadline AND the httpx read-timeout so a stalled stream raises rather than hangs.)

- [ ] **Step 6: Verify PASS + full CLI regression:** `cd "D:/download_weights" && uv run pytest tests/cli/ -v` → all pass (the rewritten watch test + new config tests + the untouched submit/list/cancel/delete/readonly tests).

- [ ] **Step 7: Tidy + commit.**
```bash
cd "D:/download_weights" && uv run ruff check --select I001 --fix src/dlw/cli/main.py src/dlw/cli/handlers.py tests/cli/test_cli_context.py tests/cli/test_cli_ops.py
git add src/dlw/cli/main.py src/dlw/cli/handlers.py tests/cli/test_cli_context.py tests/cli/test_cli_ops.py && git commit -m "feat(fu1): watch->SSE + dlw context/config commands"
```

### Task 5: M2 backend gate

- [ ] `cd "D:/download_weights" && uv run pytest -q` → all pass (failover flake = Windows-local; isolate-confirm if it appears). `python tools/lint_invariants.py --strict` → OK. No commit.

---

## Milestone M3 — docs

### Task 6: Update operator CLI docs

**Files:**
- Modify: `docs/operator/cli-sdk.md`

- [ ] **Step 1:** Document: `dlw watch` now STREAMS the task SSE (richer per-snapshot progress, self-terminating on terminal; `--interval` **deprecated** — passing it prints a stderr note and is ignored; `--timeout` bounds the deadline AND the stream read-timeout); the new `dlw context list/current/use/set` commands + the `~/.dlw/config.yaml` schema (`current_context`, `contexts.<name>.server`, `auth.<name>.access_token`). Note: the file is written **plaintext** with **best-effort `chmod 600`** — and explicitly state `chmod 600` is a **no-op on Windows** (only toggles the read-only bit; ACLs not restricted), so on Windows the token's protection is the user-profile ACL, not 600. `context current`/`list` print `token=set|unset`, never the value. Also note `context set` rewrites the YAML and **does not preserve comments** (PyYAML). Update §5/§6: move "polling watch" out of limitations (now streams); keep `login` device-flow + token-at-rest-encryption deferred.

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
