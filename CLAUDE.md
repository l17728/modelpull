# CLAUDE.md — engineering guide for modelpull

Guidance for Claude Code / contributors working in this repo. Captures the
build/test workflow, project conventions, and hard-won operational lessons so
they don't have to be re-derived. (No secrets here — credentials live only in
the deploy host's `.env`, never in git.)

## What this is

`modelpull` — a distributed HuggingFace model-weight downloader: a FastAPI
controller orchestrates parallel executors that pull from multiple sources and
write to S3-compatible storage. v2.0 (Phase 1/2/3/4) + v2.1 (15 sprints) are
implemented and merged; latest release `v2.1.0-rc.1`. See `README.md`.

Stack: Python 3.12 + FastAPI + SQLAlchemy + alembic (backend, `src/dlw/`);
Vue 3 (frontend, `frontend/`); PostgreSQL 18; MinIO/S3.

## Build / test / lint

```bash
uv sync                                   # backend deps (uv, not pip)
uv run alembic upgrade head               # DB schema (PG on :5433 in dev)
uv run uvicorn dlw.main:app --port 8001   # controller (dev)
cd frontend && pnpm install && pnpm dev   # frontend → http://localhost:5173

uv run pytest                             # backend suite
cd frontend && pnpm test                  # frontend vitest
python tools/lint_invariants.py           # 46-invariant guard
uv run ruff check src/                     # local-only style (NOT a CI gate)
```

**CI gates (what actually blocks merge):** pytest, `tools/lint_invariants.py`,
spectral + swagger-cli (on the static `api/openapi.yaml`), frontend-lint
(eslint `--max-warnings=0` + vue-tsc + vitest), frontend-build. **Backend
ruff/mypy are NOT run in CI** — keep new code clean locally but don't expect CI
to catch style.

## Conventions (don't fight these)

- **46 invariants** are declared inline + indexed in `docs/v2.0/01-architecture.md §7`
  and AST-enforced by `tools/lint_invariants.py`. Adding/removing one means
  updating the index table or CI fails.
- Every business table carries `tenant_id` (INVARIANT 8). Cross-tenant reads
  use `tenant_filtered`; a cross-tenant fetch returns **404, not 403**.
- **HF is the SHA256 source of truth** — verify cross-source downloads against
  HF's sha; loud-fail + blacklist a bad source, never trust "DB says verified".
- **Fence tokens + executor epoch** prevent double-dispatch/stale writes;
  `FOR UPDATE SKIP LOCKED` for atomic subtask claim.
- HF token never leaves the controller (reverse-proxy via `/source-proxy`).
- PostgreSQL is the only coordination plane (advisory-lock leader election,
  no etcd/redis). Executor status writes go through
  `services/state_machine.transition_executor` ONLY (lint-enforced).
- Health endpoints are **`/health/{live,ready,active}`** — there is **no
  `/healthz`** (a recurring stale reference; `/health/active` returns 200 on
  the active leader, 503 on standby).

## Deployment (single-host docker) & operational lessons

`deploy/single-host/` brings up PG + MinIO + controller (loopback `:8001`) +
2 executors + frontend (`:5173`) via `bash deploy.sh`. Front it with your own
HTTPS reverse proxy. `bootstrap.sh` generates secrets into `.env` (chmod 600,
never committed). Migrate hosts with `migrate.sh` (export/import/transfer,
`--component controller|executor|all`).

Hard-won lessons (the expensive ones):

- **`uv.lock` pins `files.pythonhosted.org` wheel URLs.** `uv sync --frozen`
  downloads from there regardless of any pip/uv index mirror — only `pip`
  benefits from a mirror, `uv sync` does not. On China-region VMs this is slow
  (~KB/s). The optimized Dockerfile splits deps-only (`--no-install-project`)
  from the project copy and adds `--mount=type=cache,target=/root/.cache/uv`
  so wheels persist/resume across builds. **Prefer not rebuilding**: for a
  one-file source patch use `docker cp` + restart; for a few files use a
  patch-layer (`FROM single-host-controller:latest` + `COPY` the changed
  files, no uv sync, seconds, no network) — it's durable (survives `docker rm`).
- **`opencode` ships as a single ELF (`opencode.exe`)**, not a JS file. The
  controller container needs `/usr/local/bin/opencode` symlinked to the
  bind-mounted package (baked into `Dockerfile.controller`); `node opencode.js`
  does not exist.
- **The webapp Docs drawer + manual are served at request time** from files
  inside the controller container (`src/dlw/api/help.py` allowlist +
  `MANUAL.md` + `docs/operator/*.md`). A deploy that ships only `src/` code
  leaves the UI serving **old docs** — ship the docs too. Docs are read per
  request (docker cp = instant); `help.py` is imported at startup (needs a
  restart to pick up an allowlist change).
- **Image swap, not restart**: `docker compose build` only updates the tag;
  `docker restart` reuses the old image. Use `docker compose up -d` to recreate
  from a new image. Build to a tag and only `up -d` after a green build so live
  containers are untouched if the build stalls.
- **Driving long remote builds over SSH (paramiko)**: a detached
  `setsid ... ` launch hangs the exec channel (the child inherits the channel
  fd), but the build keeps running. Pattern: launch detached writing to a log
  with a `BUILD_EXIT=$?` sentinel, then poll the log from *separate* short
  exec calls until the sentinel; swap + verify only on exit 0.
- China-region mirrors that work: pip/uv → Tsinghua, apt → Aliyun, Docker Hub
  → `docker.m.daocloud.io` (the only reliably-working registry mirror found).

Verification checklist after a controller deploy:
```bash
docker images | grep single-host-controller                    # tag fresh
docker exec dlw-controller sh -c 'which opencode && \
  grep -c DLW_AUTH_DEV_MODE /app/src/dlw/auth/executor_mtls.py' # baked fixes
docker exec dlw-controller curl -fsS localhost:8001/health/ready
docker logs --tail 12 dlw-controller | grep poll                # executors 200
docker exec dlw-controller sh -c 'curl -s localhost:8001/api/v1/help/docs' \
  | grep -oc '"slug"'                                            # doc count
```

## AI assistant engineering

The AI Copilot uses **opencode in headless mode** (modelpull only talks to the
`opencode` CLI; whichever LLM opencode is configured with is opencode's
concern). Tools reach the model via a **Skills bridge** (no MCP server): 21
tools (11 read + 10 write, defined in `src/dlw/ai/tools.py` +
`write_tools.py`) fed through a generated manifest; write tools require an
in-UI confirmation card; the decision chain (thinking + tool_call +
tool_result) renders above each reply. Setup/troubleshooting:
`docs/operator/runbook-ai-assistant.md`. The `docs/v2.0/12-ai-copilot.md`
design doc is frozen history (it described an MCP design that evolved into the
Skills bridge across SP4a–f).

## Observability

`/metrics` (unauthenticated) emits **12** metrics today: replication ×3,
optimizer ×1, replan ×1, controller_role, task lifecycle (active/completed/
duration), executor (count/status), subtask retries. The shipped Grafana
dashboards + Prometheus rules reference ~40 metrics — most are v2.0 design-era
and **not yet instrumented** (empty panels / never-firing alerts are expected
for those). Deploy + use guide: `docs/operator/observability.md`.

## Working style here

Autonomous, multi-step completion is expected: when executing a plan, pick the
recommended/conservative option at decision points and keep going to done
rather than stopping to confirm each step. Be honest about what's tested vs
not. Local full-suite green + CI green before considering a change done.
