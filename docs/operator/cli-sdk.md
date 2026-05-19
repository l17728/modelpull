# dlw CLI + Python SDK — Operator/User Guide (SP4)

> **Cross-references**: `docs/v2.0/11-cli-and-sdk-spec.md` (the full CLI/SDK
> vision — most of it is deferred; see §6 below) and the SP4 design doc
> `docs/superpowers/specs/2026-05-19-phase-3-sp4-cli-sdk-design.md`.

---

## 1. Install (caveat)

> ⚠️ Per `docs/v2.0/11-cli-and-sdk-spec.md` §1: the PyPI / Homebrew /
> `curl get.dlw.example.com` installers are **unreleased placeholders** —
> running them today gives `package not found`.

What works **now**:

- From the repo: `uv run dlw <command>` (e.g. `uv run dlw list`).
- After `pip install -e .` / a wheel build: the `dlw` console script is on
  `PATH` (added to `[project.scripts]`).
- SDK: `from dlw.sdk import Client, AsyncClient` (monorepo import path — see
  §4; the published-package vision's `from dlw import Client` is not used
  here because the controller owns the `dlw` package).

## 2. Auth & configuration

Token-only (no OIDC login in the MVP — that is deferred). The CLI/SDK
consume a pre-existing system-JWT (e.g. `DLW_SYSTEM_ADMIN_TOKEN` from SP1).

**Token precedence:** `--token` flag > `DLW_TOKEN` > `DLW_SYSTEM_ADMIN_TOKEN`
> `~/.dlw/config.yaml` (`auth.<current_context>.access_token`).

**Server precedence:** `--server` > `DLW_SERVER` >
`contexts.<current_context>.server` in the config file >
`http://localhost:8000`.

Config file path: `--config`/`DLW_CONFIG` > `$XDG_CONFIG_HOME/dlw/config.yaml`
> `~/.dlw/config.yaml`. A **missing config file is not an error** (env/flags
suffice — the non-interactive/CI path). A missing token → exit code 2.

## 3. CLI commands

```bash
dlw submit <repo> -r <revision> -s <storage_id> [--priority N] \
    [--strategy auto_balance] [--upgrade-from REV] [--wait] [--timeout S]
dlw list [--status STATUS]
dlw show <task_id>
dlw cancel <task_id> [--reason TEXT]
dlw delete <task_id>            # terminal tasks only (else exit 6)
dlw watch <task_id> [--interval S] [--timeout S]
```

Global: `-o/--output {table,json}` (json is the stable machine contract),
`-q/--quiet`, `--server`, `--token`, `-c/--config`, `--version`, `-h`.

Examples:

```bash
dlw -o json submit deepseek-ai/DeepSeek-V3 -r 0000…40hex -s 1
dlw list --status downloading
dlw show 7e57a3f8-…
dlw watch 7e57a3f8-… --interval 10
dlw cancel 7e57a3f8-… --reason "wrong revision"
dlw delete 7e57a3f8-…            # only if succeeded/failed/cancelled
```

**Exit codes** (POSIX, spec §4.1): `0` success · `1` generic/unexpected
(incl. a `failed` task under `--wait`) · `2` usage / missing token · `3`
not found · `4` auth/forbidden · `5` quota/rate · `6` state conflict
(e.g. `TASK_NOT_TERMINAL`) · `8` Ctrl-C · `9` `--timeout`.

## 4. Python SDK

Import path is `dlw.sdk` (monorepo: the controller owns top-level `dlw`).

Sync:

```python
from dlw.sdk import Client

with Client(server="http://localhost:8000", token="<system-jwt>") as c:
    t = c.tasks.submit(repo_id="org/model",
                        revision="<40-hex-sha>", storage_id=1)
    print(t.id, t.status)
    t = t.wait(timeout=3600,
               on_progress=lambda x: print(x.status, x.files_done()))
    for task in c.tasks.list(status="downloading"):
        print(task.repo_id)
    c.tasks.cancel(t.id)
    # c.tasks.delete(t.id)   # only when terminal
```

Async (identical surface):

```python
import asyncio
from dlw.sdk import AsyncClient

async def main():
    async with AsyncClient(server="http://localhost:8000",
                           token="<system-jwt>") as c:
        t = await c.tasks.submit(repo_id="org/model",
                                 revision="<40-hex-sha>", storage_id=1)
        t = await t.wait(timeout=3600)
        print(t.status)

asyncio.run(main())
```

Errors are typed (`dlw.sdk.errors`): `NotFound`, `AuthError`,
`QuotaExceeded`, `Conflict`, `Timeout`, `UsageError`, `ApiError` (all
subclass `DlwError`), each mapped to the CLI exit code above.

## 5. Behaviour notes

- `submit` requires `storage_id` (the controller's `TaskCreate` requires it).
- `list(status=…)` filters **client-side** (the implemented `GET
  /api/v1/tasks` has no query filter yet — see §6).
- `watch`/`wait` **poll** `GET /api/v1/tasks/{id}` until terminal
  (`succeeded`/`failed`/`cancelled`) or timeout. On an *already-terminal*
  task they emit only the final record (no progress line) — by design.
- `cancel --reason` / `cancel(reason=)` is **accepted but not persisted**
  (the cancel endpoint has no reason field yet) — reserved, no-op for now.

## 6. MVP limitations (authoritative — deferred on purpose)

1. **Client-side `list` filtering** — server-side `?status=&limit=&cursor=`
   is a future additive controller change.
2. **Polling `watch`/`wait`** — no streaming/events endpoint exists;
   `stream_events` is deferred.
3. **Token-only auth** — no OIDC `login`/`logout`/`whoami` (deferred).
4. **`cancel --reason` not persisted** — reserved (no API field).

Also deferred to later sub-projects / Phase 4: `materialize`, `search`,
`info`, `quota`, `exec`, `storage`, `audit`, `template`, `admin`,
`completion`, `--idempotency-key`, `-o yaml|wide`, Rich/Typer UX. The CLI
and SDK public surface added here is forward-compatible with adding them.

See `docs/v2.0/11-cli-and-sdk-spec.md` §6-§7 for the eventual full surface.
