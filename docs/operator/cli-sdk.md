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

### 2.1 Two operational gotchas (verified against a live deployment)

1. **Use a tenant-USER JWT to submit tasks, not the system-admin service
   token.** The admin service token resolves to `user_id=0`, and
   `download_tasks.owner_user_id` has an FK to `users` — there is no
   `User(id=0)`, so `POST /api/v1/tasks` (`dlw submit`) returns **500**
   (`fk_download_tasks_owner_user_id_users`). Mint a tenant-user JWT
   instead (the `user_id` must match a real `users` row, e.g. the seeded
   `user_id=1`):

   ```bash
   uv run python -c "from dlw.auth.principal import issue_system_jwt; \
     print(issue_system_jwt(secret='<DLW_SYSTEM_JWT_SECRET>', user_id=1, \
     tenant_id=1, role='tenant_admin', project_ids=[]))"
   ```

   The admin service token is for the admin/management plane, not task
   ownership. The JWT TTL is 1 h — re-mint on `401`.

2. **Self-signed CA: the SDK/CLI have no `--cacert`/`verify=` option**
   (documented MVP limitation). When the controller serves HTTPS with a
   private/dev CA, point httpx's default trust store at it via the
   **`SSL_CERT_FILE`** env var (httpx's default SSL context honors it):

   ```bash
   export SSL_CERT_FILE="$PWD/.ca/ca-cert.pem"
   uv run dlw --server https://localhost:8000 --token "$JWT" list
   ```

   (Raw `curl` against a self-signed controller may report `HTTP 000` in
   some shells — that is a curl/CA quirk, not a controller fault; use the
   CLI or httpx with the CA.)

> A ready-to-`source` helper that mints a fresh tenant JWT + sets
> `SSL_CERT_FILE` + `DLW_SERVER` is shown in
> [`docs/operator/local-deployment.md`](./local-deployment.md) and
> [`docs/getting-started.md`](../getting-started.md) §6.

## 3. CLI commands

```bash
dlw submit <repo> -r <revision> -s <storage_id> [--priority N] \
    [--strategy auto_balance] [--upgrade-from REV] [--wait] [--timeout S]
dlw list [--status STATUS]
dlw show <task_id>
dlw cancel <task_id> [--reason TEXT]
dlw delete <task_id>            # terminal tasks only (else exit 6)
dlw watch <task_id> [--timeout S]   # STREAMS the task SSE until terminal
dlw whoami                          # the current principal (GET /auth/me)
dlw quota                           # current tenant quota usage
dlw exec list [--status STATUS]     # registered executors
dlw events <task_id> [--limit N] [--cursor C] [--follow]
dlw audit [--action PREFIX] [--actor USER_ID] [--from DATE] [--to DATE] \
    [--limit N] [--cursor C]
dlw context list                    # contexts in ~/.dlw/config.yaml (marks current)
dlw context current                 # the active context (token redacted)
dlw context use <name>              # switch current_context
dlw context set <name> [--server URL] [--token JWT] [--no-current]
```

`dlw watch` now **streams** the task SSE (`GET /tasks/{id}/stream`): it prints a
`status done/total` line per snapshot and exits when the task is terminal (exit
`1` on `failed`). `--timeout` bounds both the client deadline and the stream
read-timeout (a stalled stream raises rather than hangs). `--interval` is
**deprecated** — passing it prints a stderr note and is ignored (the server
drives the 1 Hz tick).

`dlw context` manages `~/.dlw/config.yaml` locally (no token/network needed).
`context set` writes `contexts.<name>.server` + `auth.<name>.access_token` +
(unless `--no-current`) `current_context`. The token is stored **plaintext**;
the file is written with best-effort `chmod 600` — note this is a **no-op on
Windows** (it only toggles the read-only bit; the OS profile ACL is the actual
protection there). `context list`/`current` print `token=set|unset`, never the
value. `context set` rewrites the YAML and does not preserve comments.

`whoami`/`quota`/`exec list`/`events`/`audit` are read-only wraps of existing
endpoints (added in the SP4-CLI read-only slice). Two notes:

- **Auth scope**: `whoami` uses `require_principal` (any valid bearer, including
  the system-admin service token — which reports `user_id=0, is_service=true`),
  while `quota`/`exec`/`audit` use `require_perm` (tenant roles). So with the
  admin service token `whoami` works but `quota`/`audit` may return `403`; use a
  tenant-user JWT (§2.1) for those.
- **`watch` vs `events --follow`**: `watch` streams task *status* snapshots
  (`/tasks/{id}/stream`) and self-terminates when the task reaches a terminal
  state; `events --follow` streams the raw event log (`/events/stream`) until
  Ctrl-C / disconnect (it does NOT self-terminate). `events` (no `--follow`) is a
  one-shot paginated list.

Global: `-o/--output {table,json}` (json is the stable machine contract),
`-q/--quiet`, `--server`, `--token`, `-c/--config`, `--version`, `-h`.

Examples:

```bash
dlw -o json submit deepseek-ai/DeepSeek-V3 -r 0000…40hex -s 1
dlw list --status downloading
dlw show 7e57a3f8-…
dlw watch 7e57a3f8-… --timeout 3600
dlw context set prod --server https://dlw.example.com --token "$JWT"
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

Read-only resource methods (return the parsed JSON dict — read-only metadata,
not the typed `DownloadTask`): `c.me()`, `c.quota.current()`,
`c.executors.list(status=None)`, `c.tasks.events(task_id, limit=50, cursor=None)`,
`c.audit.search(action=None, actor_user_id=None, from_=None, to=None,
cursor=None, limit=50)`, and the SSE seams `c.tasks.events_stream(task_id,
max_ticks=None)` + `c.tasks.task_stream(task_id, max_ticks=None, timeout=None)`
(streaming context managers — `with … as r: for line in r.iter_lines(): …`).
All mirrored on `AsyncClient`. Local config helpers: `dlw.sdk._config`
exposes `load_config`/`save_config`/`set_context`/`use_context` (used by
`dlw context`).

Errors are typed (`dlw.sdk.errors`): `NotFound`, `AuthError`,
`QuotaExceeded`, `Conflict`, `Timeout`, `UsageError`, `ApiError` (all
subclass `DlwError`), each mapped to the CLI exit code above.

## 5. Behaviour notes

- `submit` requires `storage_id` (the controller's `TaskCreate` requires it).
- `list(status=…)` filters **client-side** (the implemented `GET
  /api/v1/tasks` has no query filter yet — see §6).
- `watch` **streams** `GET /api/v1/tasks/{id}/stream` (self-terminating on
  terminal status); the SDK `DownloadTask.wait()` still **polls** `GET
  /api/v1/tasks/{id}`. Both stop on `succeeded`/`failed`/`cancelled` or timeout;
  an *already-terminal* task yields the final record immediately.
- `cancel --reason` / `cancel(reason=)` is **accepted but not persisted**
  (the cancel endpoint has no reason field yet) — reserved, no-op for now.

## 6. MVP limitations (authoritative — deferred on purpose)

**Now available** (read-only slice, added after the original SP4 MVP):
`whoami`, `quota` (the `usage` subcommand is still deferred — bare `dlw quota`
is the `show` view), `exec list`, `events [--follow]`, `audit` — and the SDK
methods `me`/`quota.current`/`executors.list`/`tasks.events`/
`tasks.events_stream`/`audit.search`/`task_stream`. So the earlier "no events
endpoint", "no `quota`/`exec`/`audit`", and "polling `watch`" limitations are
lifted: `watch` now streams the task SSE, `events --follow` streams the event
log, and `dlw context list/current/use/set` manage `~/.dlw/config.yaml`.

Still deferred:

1. **Client-side `list` filtering** — server-side `?status=&limit=&cursor=`
   is a future additive controller change.
2. **OIDC `login`/`logout`** — the controller's OIDC is a browser
   authorization-code redirect flow; a CLI needs a **device-code flow**
   endpoint (`POST /auth/device`) that does not exist. `whoami`/`context set`
   (persisting a token you already have) work today; `login` does not.
3. **Arbitrary config keys (`config get/set`, defaults) + secure token-at-rest**
   — `dlw context` manages server/token contexts (plaintext, chmod-600
   best-effort; no-op on Windows). General config-key get/set and encrypted token
   storage are deferred.
4. **`cancel --reason` not persisted** — reserved (no API field).

Also deferred to later sub-projects / Phase 4: `materialize`, `search`,
`info`, `retry`, `upgrade`, `storage`, `template`, `admin`, `completion`,
`--idempotency-key`, `-o yaml|wide`, Rich/Typer UX — these have no implemented
controller endpoints (or need a byte/executor path). The CLI and SDK public
surface is forward-compatible with adding them.

See `docs/v2.0/11-cli-and-sdk-spec.md` §6-§7 for the eventual full surface.
