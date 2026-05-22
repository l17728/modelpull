# FU7 — `dlw config get/set` arbitrary keys + CLI defaults

## Problem

`dlw context` manages server/token contexts, but there is no general
config get/set and no `defaults:` section, so every `dlw submit` must pass
`--storage` (a `required=True` flag) and there's no way to persist a preferred
output format. The operator doc's deferral (`docs/operator/cli-sdk.md:290`):

> **Arbitrary config keys (`config get/set`, defaults) + secure token-at-rest**
> … General config-key get/set and encrypted token storage are deferred.

FU7 lifts the first half (config get/set + defaults). FU8 will lift token-at-rest.

The v2.0 spec (`docs/v2.0/11-cli-and-sdk-spec.md:136`) names `dlw config
[get|set|edit]` and a `defaults:` block (`:50-77`) with `storage_id`,
`source_strategy`, `priority`, `output`, `color`; precedence (`:14`) is
**flag > env > config file > default**. The exact CLI syntax is unspecified —
FU7 decides it.

## §0 Design — pure CLI/config, zero backend

### Config schema addition

A top-level `defaults:` block in `~/.dlw/config.yaml` (alongside the existing
`current_context`/`contexts`/`auth`):
```yaml
defaults:
  storage_id: 5
  source_strategy: auto_balance
  priority: 1
  output: table          # table | json
```

### `_config.py` — dotted-key helpers (additive; nothing existing changes)

- `get_config_value(key: str, *, config_path=None) -> Any | None` — dotted-path
  read (`"defaults.storage_id"` → `cfg["defaults"]["storage_id"]`); missing → None.
- `set_config_value(key: str, value, *, config_path=None) -> Path` — dotted-path
  set, creating intermediate dicts; `save_config`. `value` is already the typed
  Python scalar (the CLI coerces the argv string — see below).
- `unset_config_value(key: str, *, config_path=None) -> bool` — remove the dotted
  leaf (and return whether it existed); prune now-empty parent dicts is NOT done
  (keep it simple — an empty `{}` is harmless).
- `flatten_config(cfg: dict) -> dict[str, Any]` — recursively flatten to dotted
  keys for `config list`.
- `get_default(name: str, *, config_path=None) -> Any | None` — thin alias of
  `get_config_value(f"defaults.{name}", ...)`.

**Token redaction:** `config get`/`list` MUST redact any key whose leaf is
`access_token` (print `***` / `set`), mirroring how `context list` never prints
the token value.

### CLI — `dlw config` subcommand (mirrors `dlw context`)

Dispatched in `handlers.run()` **before** `make_client` (pure local config op):

- `dlw config get <key>` — print the value; `access_token` leaves redacted;
  missing key → print nothing + exit 0 (or `(unset)`); never raise.
- `dlw config set <key> <value>` — coerce `<value>` with `yaml.safe_load` so
  `5`→int, `true`→bool, `auto_balance`→str (documented), then
  `set_config_value`. Print `set <key> = <value>` unless `--quiet`.
- `dlw config unset <key>` — remove; print confirmation.
- `dlw config list` — print every dotted key = value (tokens redacted), with the
  config-path header (like `context list`).

### Wiring `defaults.*` into CLI behavior (the actual value)

Precedence everywhere: **explicit flag > env > config `defaults.*` > hardcoded**.
To detect "explicit flag", the affected flags switch to `default=None` and the
handler resolves:

- **`submit --storage`** — currently `type=int, required=True`. Change to
  `type=int, default=None` (NOT required). In the submit handler:
  `storage_id = args.storage if args.storage is not None else get_default("storage_id", config_path=args.config)`;
  if still `None` → `raise UsageError("no storage_id: pass --storage or set "
  "defaults.storage_id (dlw config set defaults.storage_id <N>)")` (exit 2).
- **`submit --priority`** — `default=None`; resolve
  `args.priority if not None else get_default("priority") or 1`.
- **`submit --strategy`** — `default=None`; resolve
  `args.strategy if not None else get_default("source_strategy") or "auto_balance"`.
- **global `-o/--output`** — `choices=["table","json"], default=None`. Resolve once
  in `main()` after `parse_args` (before `_dispatch`):
  `args.output = _resolve_output(args.output, args.config)` where
  `_resolve_output = flag > os.environ["DLW_OUTPUT"] > defaults.output > "table"`,
  and a config/env value not in `{table, json}` falls back to `table` (yaml output
  is not implemented — documented). `_print_err`'s `args.output == "json"` check
  still works because `args.output` is resolved before any dispatch.

### Explicitly NOT in scope (documented deferrals)

- `defaults.color` (auto/always/never) — the CLI does not colorize output today;
  `config set defaults.color X` is *stored* (arbitrary key) but inert. Wiring color
  is a follow-on.
- `defaults.output: yaml` — only `table`/`json` are rendered; `yaml` falls back to
  `table`.
- `dlw config edit` (open `$EDITOR`) — deferred; `get`/`set`/`unset`/`list` cover
  the need.
- Arbitrary keys outside `defaults.*` can be stored/read (it's a general dotted
  get/set), but only the four `defaults.*` keys above change CLI behavior.

## §1 Tests (all pure-CLI, no backend)

`tests/cli/test_cli_config.py` (mirror `test_cli_context.py`'s
`cli.main(["-c", str(tmp), ...])` harness):
- `set`→`get` round-trip: `config set defaults.storage_id 5` then
  `config get defaults.storage_id` prints `5` (int, not `"5"`); `config list`
  shows it.
- string value: `config set defaults.source_strategy round_robin` → `get` prints
  `round_robin`.
- `unset`: after set then `config unset defaults.priority`, `get` prints nothing.
- redaction: `config set auth.prod.access_token SECRET` then `config get
  auth.prod.access_token` does NOT print `SECRET` (prints `***`/`set`); `config
  list` likewise.
- **submit default wiring** (reuse the mock-transport harness from
  `tests/cli/test_cli_submit_show.py`: `monkeypatch.setattr(cli, "_transport",
  <mock>)` + `DLW_TOKEN`): with `config set defaults.storage_id 7`, run `submit
  repo -r rev` (NO `--storage`) → asserts the POSTed body `storage_id == 7`.
- precedence: `--storage 9` with `defaults.storage_id 7` → body `storage_id == 9`.
- missing: `submit repo -r rev` with no `--storage` and no default → exit 2
  (UsageError).
- output default: `config set defaults.output json` then a command (e.g. a
  mock-backed `list`) emits JSON; and `_resolve_output` unit cases (flag>env>config,
  unknown→table). `DLW_OUTPUT=json` env beats config; `-o table` flag beats both.

## §2 Milestones

- **M1** — `_config.py` dotted helpers + `config` subcommand + `config` tests.
  Gate: `pytest tests/cli/test_cli_config.py -q`.
- **M2** — wire `defaults.*` into `submit` + global `-o` resolution + wiring tests
  + docs (`docs/operator/cli-sdk.md`). Gate: full `pytest -q` (esp. existing
  `test_cli_submit_show.py` / `test_cli_readonly.py` stay green),
  `lint_invariants --strict`.

## §3 Notes

- Zero migration, no openapi change, no backend/SDK-`Client` change, no frontend.
  Only `src/dlw/sdk/_config.py` + `src/dlw/cli/main.py` + `src/dlw/cli/handlers.py`
  + tests + docs.
- The `default=None`→resolve change for `--storage`/`--priority`/`--strategy`/`-o`
  must keep existing tests green (the resolver's fallback reproduces the old
  hardcoded defaults). Run the full CLI test suite at M2.
