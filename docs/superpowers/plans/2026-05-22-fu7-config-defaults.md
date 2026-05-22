# FU7 — `dlw config get/set` + CLI defaults Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: subagent-driven-development / executing-plans. Checkbox steps.

**Goal:** Add `dlw config get/set/unset/list` for dotted config keys + a `defaults:` block, and wire `defaults.{storage_id,priority,source_strategy,output}` into the CLI so flags can be omitted (precedence flag > env > config > hardcoded).

**Spec:** `docs/superpowers/specs/2026-05-22-fu7-config-defaults-design.md` (read fully).

**Locked constraints:**
- Pure CLI/config: ONLY `src/dlw/sdk/_config.py`, `src/dlw/cli/main.py`, `src/dlw/cli/handlers.py`, tests, docs. Zero migration / openapi / backend / SDK-`Client` / frontend.
- `config get`/`list` redact any key whose leaf is `access_token`.
- `set` coerces the argv string via `yaml.safe_load` (5→int, true→bool, str→str).
- Switching `--storage`/`--priority`/`--strategy`/`-o` to `default=None`+resolve MUST keep existing CLI tests green (resolver reproduces old defaults).
- CI gate = pytest + `lint_invariants`; `ruff --select I001 --fix` touched files.

---

## File Structure
- **Modify** `src/dlw/sdk/_config.py` (5 dotted helpers).
- **Modify** `src/dlw/cli/main.py` (`config` subparser; `--storage`/`--priority`/`--strategy`/`-o` → default None; `_resolve_output` in `main()`).
- **Modify** `src/dlw/cli/handlers.py` (`_config_cmd` + dispatch; submit default resolution).
- **Create** `tests/cli/test_cli_config.py`.
- **Modify** `docs/operator/cli-sdk.md`.

---

## Milestone M1 — config get/set/unset/list

### Task 1: `_config.py` dotted helpers + `config` subcommand
**Files:** `src/dlw/sdk/_config.py`, `src/dlw/cli/main.py`, `src/dlw/cli/handlers.py`, `tests/cli/test_cli_config.py`.

- [ ] **Step 1 (failing tests):** create `tests/cli/test_cli_config.py` mirroring `tests/cli/test_cli_context.py` (`from dlw.cli import main as cli`; `cli.main(["-c", str(tmp_path/"c.yaml"), ...])`; `capsys`). Tests:
  - `test_set_get_roundtrip_int`: `config set defaults.storage_id 5` (exit 0); `config get defaults.storage_id` → stdout contains `5`; load the yaml directly and assert `cfg["defaults"]["storage_id"] == 5` (int).
  - `test_set_get_string`: `config set defaults.source_strategy round_robin`; `config get defaults.source_strategy` → `round_robin`.
  - `test_unset`: set `defaults.priority 3`, then `config unset defaults.priority` (exit 0), then `config get defaults.priority` → no value printed.
  - `test_list_shows_keys`: after a couple sets, `config list` stdout has `defaults.storage_id` and its value.
  - `test_token_redacted`: `config set auth.prod.access_token SECRET`; `config get auth.prod.access_token` stdout does NOT contain `SECRET`; `config list` stdout does NOT contain `SECRET`.
- [ ] **Step 2: verify FAIL** (no `config` cmd / helpers).
- [ ] **Step 3 (_config.py helpers):** add after `clear_token`:
```python
def _split(key: str) -> list[str]:
    return [p for p in key.split(".") if p]


def get_config_value(key: str, *, config_path: str | None = None):
    cur = _load_config(config_path) or {}
    for p in _split(key):
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur


def set_config_value(key: str, value, *, config_path: str | None = None) -> Path:
    cfg = _load_config(config_path) or {}
    parts = _split(key)
    cur = cfg
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value
    return save_config(cfg, config_path=config_path)


def unset_config_value(key: str, *, config_path: str | None = None) -> bool:
    cfg = _load_config(config_path) or {}
    parts = _split(key)
    cur = cfg
    for p in parts[:-1]:
        cur = cur.get(p) if isinstance(cur, dict) else None
        if not isinstance(cur, dict):
            return False
    existed = isinstance(cur, dict) and parts[-1] in cur
    if existed:
        del cur[parts[-1]]
        save_config(cfg, config_path=config_path)
    return existed


def flatten_config(cfg: dict) -> dict:
    out: dict = {}
    def _walk(prefix: str, node) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                _walk(f"{prefix}.{k}" if prefix else k, v)
        else:
            out[prefix] = node
    _walk("", cfg)
    return out


def get_default(name: str, *, config_path: str | None = None):
    return get_config_value(f"defaults.{name}", config_path=config_path)
```
- [ ] **Step 4 (parser):** in `cli/main.py` add a `config` subparser mirroring `context` (registered right after the `context` block):
```python
    cfgp = sub.add_parser("config", help="get/set CLI config keys + defaults")
    cfg_sub = cfgp.add_subparsers(dest="config_cmd")
    cfg_get = cfg_sub.add_parser("get", help="print a dotted config key")
    cfg_get.add_argument("key")
    cfg_set = cfg_sub.add_parser("set", help="set a dotted config key")
    cfg_set.add_argument("key")
    cfg_set.add_argument("value")
    cfg_unset = cfg_sub.add_parser("unset", help="remove a dotted config key")
    cfg_unset.add_argument("key")
    cfg_sub.add_parser("list", help="list all config keys (tokens redacted)")
```
- [ ] **Step 5 (handler):** in `cli/handlers.py` add `_config_cmd(args)` and dispatch it at the TOP of `run()` (next to `context`/`login`/`logout`, before `make_client`):
```python
def _redact(key: str, value):
    return "***" if key.split(".")[-1] == "access_token" else value


def _config_cmd(args) -> int:
    import yaml
    from dlw.sdk import _config as cfgmod
    cp = args.config
    sub = getattr(args, "config_cmd", None)
    if sub == "set":
        val = yaml.safe_load(args.value)   # 5->int, true->bool, str->str
        cfgmod.set_config_value(args.key, val, config_path=cp)
        if not args.quiet:
            sys.stdout.write(f"set {args.key} = {_redact(args.key, val)}\n")
        return 0
    if sub == "unset":
        existed = cfgmod.unset_config_value(args.key, config_path=cp)
        if not args.quiet:
            sys.stdout.write(
                f"{'unset' if existed else 'no such key'} {args.key}\n")
        return 0
    if sub == "get":
        v = cfgmod.get_config_value(args.key, config_path=cp)
        if v is not None:
            sys.stdout.write(f"{_redact(args.key, v)}\n")
        return 0
    if sub == "list":
        cfg = cfgmod.load_config(cp)
        sys.stdout.write(f"# config: {cfgmod._resolve_write_path(cp)}\n")
        flat = cfgmod.flatten_config(cfg)
        if not flat:
            sys.stdout.write("(empty)\n")
        for k in sorted(flat):
            sys.stdout.write(f"{k} = {_redact(k, flat[k])}\n")
        return 0
    sys.stderr.write("usage: dlw config [get KEY|set KEY VALUE|unset KEY|list]\n")
    return 2
```
Add `if args.cmd == "config": return _config_cmd(args)` alongside the existing `context` dispatch at the top of `run()`.
- [ ] **Step 6: verify PASS** — `cd "D:/download_weights" && uv run pytest tests/cli/test_cli_config.py -v` all pass.
- [ ] **Step 7: tidy + commit.**
```bash
cd "D:/download_weights" && uv run ruff check --select I001 --fix src/dlw/sdk/_config.py src/dlw/cli/main.py src/dlw/cli/handlers.py tests/cli/test_cli_config.py
git add src/dlw/sdk/_config.py src/dlw/cli/main.py src/dlw/cli/handlers.py tests/cli/test_cli_config.py && git commit -m "feat(fu7): dlw config get/set/unset/list (dotted keys, token-redacted)"
```

### Task 2: M1 gate
- [ ] `cd "D:/download_weights" && uv run pytest -q` all pass (failover flake = Windows-local); `uv run python -m dlw.tools.lint_invariants --strict` OK. No commit.

---

## Milestone M2 — wire defaults into submit + output

### Task 3: defaults resolution + `-o` resolution + docs
**Files:** `src/dlw/cli/main.py`, `src/dlw/cli/handlers.py`, `tests/cli/test_cli_config.py`, `docs/operator/cli-sdk.md`.

- [ ] **Step 1 (failing tests):** extend `tests/cli/test_cli_config.py`, reusing the mock-transport harness from `tests/cli/test_cli_submit_show.py` (read it: it sets `monkeypatch.setattr(cli, "_transport", <mock>)` + `monkeypatch.setenv("DLW_TOKEN", <tok>)`; the mock must capture the POSTed body for `/api/v1/tasks`). Tests:
  - `test_submit_uses_default_storage`: `config set defaults.storage_id 7` (via cli.main with the same `-c` tmp), then `cli.main(["-c", tmp, "submit", "repo/x", "-r", "main"])` (NO `--storage`) → exit 0 and the captured POST body has `storage_id == 7`.
  - `test_submit_flag_beats_default`: with `defaults.storage_id 7`, `submit repo/x -r main -s 9` → body `storage_id == 9`.
  - `test_submit_missing_storage_errors`: no default, no `--storage` → exit 2.
  - `test_output_default_json`: `config set defaults.output json`, then a mock-backed `list` → stdout is JSON (starts with `[` or `{`). Also a direct unit test of `_resolve_output`: flag `"table"` beats env `DLW_OUTPUT=json` beats config `json` beats `None→"table"`; an unknown value (`"yaml"`) → `"table"`.
- [ ] **Step 2: verify FAIL.**
- [ ] **Step 3 (parser defaults→None):** in `cli/main.py`:
  - `-o/--output`: `choices=["table","json"], default=None` (was `"table"`).
  - submit `-s/--storage`: `type=int, default=None` (REMOVE `required=True`).
  - submit `--priority`: `type=int, default=None` (was `1`).
  - submit `--strategy`: `default=None` (was `"auto_balance"`).
- [ ] **Step 4 (`_resolve_output` + main):** in `cli/main.py` add:
```python
def _resolve_output(flag: str | None, config_path: str | None) -> str:
    import os
    from dlw.sdk._config import get_default
    val = flag or os.environ.get("DLW_OUTPUT") or get_default(
        "output", config_path=config_path)
    return val if val in ("table", "json") else "table"
```
In `main()`, right after `args = parser.parse_args(argv)` and the `if not args.cmd` guard, set `args.output = _resolve_output(args.output, args.config)` so every downstream `args.output` check (emit + `_print_err`) sees the resolved value.
- [ ] **Step 5 (submit resolution in handler):** in `cli/handlers.py` `run()` submit branch, before `client.tasks.submit(...)`:
```python
            from dlw.sdk._config import get_default
            storage_id = (args.storage if args.storage is not None
                          else get_default("storage_id", config_path=args.config))
            if storage_id is None:
                from dlw.sdk.errors import UsageError
                raise UsageError(
                    "no storage_id: pass --storage or set defaults.storage_id "
                    "(dlw config set defaults.storage_id <N>)")
            priority = (args.priority if args.priority is not None
                        else get_default("priority", config_path=args.config) or 1)
            strategy = (args.strategy if args.strategy is not None
                        else get_default("source_strategy", config_path=args.config)
                        or "auto_balance")
```
then pass `storage_id=storage_id, priority=priority, source_strategy=strategy` to `client.tasks.submit(...)` (replace the `args.*` references in that call).
- [ ] **Step 6 (docs):** `docs/operator/cli-sdk.md`: add `dlw config get/set/unset/list` to the command listing (near `context`), document the `defaults:` YAML block + the four wired keys + precedence (flag > env > config > default) + that `set` parses values as YAML scalars + tokens are redacted on read. Update the deferral note at ~line 290: FU7 lifts the config-key get/set + defaults half (note `color`/`yaml`-output/`config edit` remain deferred); token-at-rest still deferred (FU8).
- [ ] **Step 7: verify PASS** — `cd "D:/download_weights" && uv run pytest tests/cli/test_cli_config.py tests/cli/test_cli_submit_show.py tests/cli/test_cli_readonly.py -v` all pass.
- [ ] **Step 8: tidy + commit.**
```bash
cd "D:/download_weights" && uv run ruff check --select I001 --fix src/dlw/cli/main.py src/dlw/cli/handlers.py tests/cli/test_cli_config.py
git add src/dlw/cli/main.py src/dlw/cli/handlers.py tests/cli/test_cli_config.py docs/operator/cli-sdk.md && git commit -m "feat(fu7): wire defaults.{storage_id,priority,source_strategy,output} into CLI"
```

### Task 4: M2 full gate
- [ ] `cd "D:/download_weights" && uv run pytest -q` ALL pass (failover flake = Windows-local; isolate-confirm); `lint_invariants --strict` OK. No commit.

---

## Self-Review
- **Spec coverage:** §0 helpers → Task 1 Step 3 ✓; `config` cmd → Task 1 Steps 4-5 ✓; submit wiring → Task 3 Step 5 ✓; output resolution → Task 3 Step 4 ✓; redaction → `_redact` ✓; tests → Tasks 1,3 ✓; §2 milestones → M1/M2 ✓.
- **Placeholder scan:** Task 3 Step 1 references reading `test_cli_submit_show.py` for the mock-transport body-capture idiom — a real lookup, not a TODO; the assertions are concrete.
- **Type consistency:** `set` coerces via `yaml.safe_load` → typed scalar; `get`/`get_default` return that scalar; `submit` passes `storage_id:int`. `_resolve_output` returns one of `{table,json}`. `_redact(key, value)` keyed on leaf `access_token`.
- **Open risks for reviewers:** (a) changing `--storage required=True`→optional + `-o`/`--priority`/`--strategy` default None — do ALL existing CLI tests still pass (the resolver must reproduce old defaults)? (b) `args.output` resolved in `main()` before dispatch — does `_print_err`'s `args.output == "json"` still work (yes, resolved to a concrete value first)? (c) `yaml.safe_load` on a value like `off`/`no`/`~` yields surprising types (bool/None) — acceptable & documented? (d) redaction only on leaf == `access_token` — any other secret leaf? (tokens are the only secret in this config). (e) `config get` on a missing key prints nothing + exit 0 — acceptable (not exit-3)?
