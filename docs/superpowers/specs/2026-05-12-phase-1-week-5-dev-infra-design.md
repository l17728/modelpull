# Phase 1 Week 5 — Dev Infra (slim) Design

> Three independent deliverables that close real Phase 1 engineering gaps
> without prematurely introducing Phase 2/3 surface area:
> reproducible fixture generator, PG-TPS baseline (P-005), and a pinned
> demo dataset catalog. Companion to roadmap §1.6 Week 5 with explicit
> YAGNI deferrals for CI runner / admin CLI / enrollment CLI.

- **Status**: design approved (2026-05-12)
- **Phase**: Phase 1, Week 5 (slim — "FEAS-06 forgotten tasks" subset)
- **Source roadmap**: `docs/v2.0/08-mvp-roadmap.md` §1.6 Week 5
- **Companion specs**: `docs/v2.0/07-test-plan.md` §perf row P-005 (PG TPS target = 5000 commits/s)
- **Pre-flight**: PR #5 merged to main (`c952957`). PR #6 (alpha demo) + PR #7 (P2-W1 fence) in flight — orthogonal; this plan branches off main `c952957` independently.
- **Author**: l17728
- **Reviewer**: TBD (pre-execution multi-agent review pass)

---

## 1. Goal & Non-Goals

### 1.1 Goal

Close the three Phase-1-relevant gaps from roadmap §1.6 Week 5:

1. **Reproducible fixture generator** — extract the 4-row seed (tenant/project/user/storage) currently duplicated across test modules into a single Python module + `dlw-seed` CLI. Enables: alpha demo seed-on-boot, easier dev onboarding, less test drift.
2. **P-005 PG TPS baseline** — capture pgbench data on the dev PG so Phase 2 W2 has the §2.4 entry-criterion artefact. Phase 2 entry says *"Phase 1 实测 P-005 数据存在；如不达标先优化"* — this plan creates that data.
3. **Demo dataset catalog** — pin specific HuggingFace revision SHAs for the alpha demo model + 2 alternates so demos / smoke / Phase 2 chunk-level benchmarks all reference the same content-addressed artefacts.

### 1.2 Non-goals (explicit Phase 1 YAGNI deferrals)

Roadmap §1.6 Week 5 listed 5 items; 3 are deferred because their prerequisites belong to later phases:

| Item | Deferred to | Reason |
|------|-------------|--------|
| Self-hosted CI runner setup | Phase 2 W2 chunk-level benchmark | Phase 1 CI uses mock HF + moto S3; no large-file E2E is run on GH-hosted. Self-hosted only becomes a hard requirement when Phase 2 introduces multi-GB benchmarks. |
| `dlw admin executor-token` CLI (FEAS-03) | Phase 2 W3 mTLS plan | `enrollment_token` is an mTLS bootstrap artefact. Phase 1 authenticates executors via the shared `DLW_BEARER_TOKEN`; no enrollment surface yet. |
| `dlw admin` multi-user CLI (FEAS-04) | Phase 3 OIDC plan | `tenant_admin` / `system_admin` only have meaning under multi-tenant RBAC; Phase 1 hardcodes `tenant_id=1`. |
| OIDC IdP setup docs | Already shipped | `docs/operator/oidc-setup.md` was written in v2.0.13 — nothing to do here. |
| Multi-model demo runs against real HF | Phase 2 ChecksumSHA256 validation | Phase 1 alpha demo's ~90 MB single model is enough to prove the streaming pipeline. |
| pgbench business-workload script | Phase 2 W2 multi-executor scheduler benchmark | Phase 1's P-005 baseline uses standard TPC-B-like workload (industry comparable). Business mix matters only when Phase 2 introduces real concurrency. |
| Performance baseline P-001/P-002/P-003/P-004 | Phase 2 / Phase 3 entry checks | This plan only captures P-005 (PG TPS). The other perf baselines have different acceptance gates and tooling. |

---

## 2. Tech Stack Additions

No runtime dependencies. One project-script entry-point added to `pyproject.toml`.

| Concern | Choice | Notes |
|---------|--------|-------|
| Fixture generator | Plain Python + SQLAlchemy 2.x async | Reuses existing models / session. ON CONFLICT DO NOTHING for idempotency. |
| CLI | `argparse` + `asyncio.run` | Same pattern as `dlw-executor` (W3); no Click / Typer for Phase 1 single-flag CLI. |
| pgbench | OS package (`postgresql-client` / `postgresql-contrib`) | Standard tool; ships with PostgreSQL. Not a Python dep. |
| Demo dataset pinning | `huggingface-cli repo info --field sha` | Human-run during plan execution; SHAs hardcoded into the markdown catalog. |
| Tests | Existing pytest + pytest-asyncio | Plus `subprocess.run` for CLI tests (mirrors W3 `dlw-executor` test pattern). |

---

## 3. Components

### 3.1 New: `dlw.fixtures` module

```python
# src/dlw/fixtures.py — NEW
from dataclasses import dataclass

@dataclass(frozen=True)
class TenantSeed:
    id: int = 1
    slug: str = "default"
    display_name: str = "Default Tenant"

@dataclass(frozen=True)
class ProjectSeed:
    id: int = 1
    tenant_id: int = 1
    name: str = "default"

@dataclass(frozen=True)
class UserSeed:
    id: int = 1
    tenant_id: int = 1
    oidc_subject: str = "dev-user"
    email: str = "dev@local"
    role: str = "tenant_admin"

@dataclass(frozen=True)
class StorageSeed:
    id: int = 1
    tenant_id: int = 1
    name: str = "default"
    backend_type: str = "s3"
    region: str = "us-east-1"
    config: dict | None = None      # None → empty bytes; dict → JSON-serialised


async def seed_default(
    session,
    *,
    tenant: TenantSeed = TenantSeed(),
    project: ProjectSeed = ProjectSeed(),
    user: UserSeed = UserSeed(),
    storage: StorageSeed = StorageSeed(),
) -> None:
    """Insert the standard Phase 1 4-row seed. Idempotent via ON CONFLICT DO NOTHING.

    Caller commits.
    """


async def seed_demo_data(session) -> None:
    """seed_default + create one pending DownloadTask pointing at the
    alpha demo model. Idempotent.
    """
```

Key properties:

- **Idempotent** via PG `INSERT ... ON CONFLICT (id) DO NOTHING` — running the seeder twice never errors, never duplicates rows.
- **Caller commits** — matches Phase 1 W2 service-layer convention (`scheduler.complete_subtask` etc. don't commit internally).
- **Dataclass arguments** so future Phase 3 multi-tenant variants can take e.g. `tenants: list[TenantSeed]` without breaking the Phase 1 interface.
- The Phase-1 `tenant_id=1` hardcoded everywhere in `api/tasks.py` etc. is preserved; the seed dataclass defaults match.

### 3.2 New: `dlw-seed` CLI

```python
# src/dlw/cli/seed.py — NEW
# Entry point: dlw-seed = "dlw.cli.seed:main"
#
# Usage:
#   dlw-seed --default      # tenant=1 + project=1 + user=1 + storage=1
#   dlw-seed --demo         # +1 pending DownloadTask (sentence-transformers/all-MiniLM-L6-v2)
```

- argparse with a `mutually_exclusive_group(required=True)` so a user must pick one flag (no "default to default"; avoids accidental seeding in prod).
- Reads DB config from existing `dlw.config.get_settings()` — same env vars as the controller.
- Async wrapper: `asyncio.run(_async_main(args))`.
- Cleans up via `await reset_engine()` in a `finally` block (matches W4 lifespan pattern).

### 3.3 NOT Modified: `tests/conftest.py`

The current `tests/conftest.py` does NOT centralise the `env` fixture — each test module defines its own (e.g. `tests/services/test_task_service.py`, `tests/api/test_tasks.py`, etc.). They inline 4 `session.add()` calls. Switching every module to call `seed_default()` would touch 5–6 test files and risk conflicting with PR #6 (alpha demo) and PR #7 (P2-W1 fence-token), both in flight on different branches.

**Decision:** this plan ships `dlw.fixtures.seed_default()` as the canonical reusable seed but does NOT refactor any existing test fixture. After PR #6 and PR #7 merge, a one-line cleanup pass can switch each module's `env` over (each becomes `await seed_default(db_session); await db_session.flush()`). Phase 1 W6 (alpha buffer) is the natural place.

The new test files this plan adds (`tests/test_fixtures.py`, `tests/cli/test_seed_cli.py`) use `seed_default()` directly — those are written fresh against the new module.

### 3.4 New: `scripts/bench-pg-tps.sh`

Bash wrapper around `pgbench`:

1. Drop + recreate a dedicated `pgbench_p005` DB (so the test never touches `dlw` production schema).
2. `pgbench -i -s 10` initialise (scale 10 ≈ 150 MB).
3. `pgbench -c 10 -j 4 -T 60` standard TPC-B-like workload.
4. `tee` stdout into `docs/perf-baselines/p-005-pg-tps.txt` (raw — gitignored).
5. Print next-step instruction to fill the markdown report.

Parameters overridable via env: `PGBENCH_SECONDS`, `PGBENCH_CLIENTS`, `PGBENCH_JOBS`, `PGBENCH_SCALE`, `PGHOST`, `PGPORT`, `PGUSER`.

### 3.5 New: `docs/perf-baselines/p-005-pg-tps.md`

Human-curated baseline report. Sections:

- Methodology (link to script)
- Environment (CPU / mem / disk / PG version / fsync settings)
- Result (paste pgbench tail showing sustained TPS)
- Assessment vs target 5000 commits/s
- What this does NOT measure (modelpull's actual write mix; multi-executor concurrency; read load)
- Reproduce (`./scripts/bench-pg-tps.sh`)

### 3.6 New: `docs/demo/dataset-catalog.md`

Three entries:

1. **Alpha demo default**: `sentence-transformers/all-MiniLM-L6-v2` @ pinned SHA (~91 MB, 9 files). Used by `scripts/demo-alpha.sh` (PR #6) + manual smoke `tests/e2e/test_hf_s3_smoke_local.py`.
2. **Faster smoke**: `prajjwal1/bert-tiny` @ pinned SHA (~17 MB). For ad-hoc dev iteration.
3. **Phase 2+ catalogued**: `deepseek-ai/DeepSeek-V3` (689 GB, 163 files). Not downloaded in Phase 1; documented for future chunk-level benchmark planning.

Pin command:

```bash
huggingface-cli repo info <repo_id> --revision main --field sha
```

---

## 4. Schema Changes

**None.** This plan touches no DB schema. No alembic migration.

---

## 5. CLI Interface

```
$ dlw-seed --help
usage: dlw-seed [-h] (--default | --demo)

options:
  --default      Insert the 4-row Phase 1 default seed (idempotent)
  --demo         seed_default + 1 demo task (idempotent)

$ dlw-seed --default
[dlw-seed] default seed applied.

$ dlw-seed --demo
[dlw-seed] demo seed applied.

$ dlw-seed
usage: dlw-seed [-h] (--default | --demo)
dlw-seed: error: one of the arguments --default --demo is required
$ echo $?
2
```

### 5.1 Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success (rows inserted or idempotent no-op) |
| 1 | DB connection failure / unexpected exception |
| 2 | argparse error (missing required flag) |

### 5.2 DB config source

Same as the controller — `dlw.config.get_settings()` reads `DLW_DB_HOST` / `DLW_DB_PORT` / `DLW_DB_USER` / `DLW_DB_PASSWORD` / `DLW_DB_NAME` env vars (defaults: `localhost:5433` / `postgres` / `""` / `dlw`).

---

## 6. Error Handling

| Source | Trigger | Handling |
|--------|---------|----------|
| `dlw-seed` no flag | argparse `mutually_exclusive_group(required=True)` rejects | exit 2 + usage to stderr |
| `dlw-seed` against DB without migrations | `seed_default` references columns/tables that don't exist → SQLAlchemy raises | propagate; user runs `alembic upgrade head` first |
| `dlw-seed` against DB with existing rows | `ON CONFLICT DO NOTHING` skips | success, no diagnostic noise |
| `bench-pg-tps.sh` without pgbench in PATH | `command -v pgbench` check at start | exit 2 + install hint |
| `bench-pg-tps.sh` createdb fails | `set -e` aborts the script | bash exit non-zero |
| Demo catalog SHA drift (HF re-tags) | None — SHA pinning is the whole point | If HF tombstones a revision, manual catalog update + new SHA |

---

## 7. Testing Strategy

### 7.1 Unit + integration (CI required)

```
tests/test_fixtures.py                       [NEW]
  test_seed_default_inserts_four_rows         # COUNT(*) on tenants/projects/users/storage = 1
  test_seed_default_idempotent_via_on_conflict  # call twice, still 4 rows; no IntegrityError
  test_seed_default_with_custom_storage_config  # StorageSeed(config={...}) → JSON bytes
  test_seed_demo_data_creates_pending_task    # 1 DownloadTask row, status='pending'

tests/cli/test_seed_cli.py                   [NEW]
  test_seed_cli_default_runs_in_subprocess    # subprocess.run dlw-seed --default; exit 0
  test_seed_cli_demo_runs_in_subprocess       # subprocess.run dlw-seed --demo; exit 0
  test_seed_cli_no_arg_errors                 # argparse exits 2
```

Pattern follows `tests/executor/test_cli.py` from W3: spin a subprocess against the per-session test DB. The CLI reads `DLW_DB_HOST` / `DLW_DB_PORT` / `DLW_DB_NAME` etc. via `dlw.config.get_settings()`; tests pipe these through `env=` of `subprocess.run`, using `test_db_name` fixture + `_pg_env()` helper from conftest. After the subprocess exits, the test opens its own `db_session` and asserts the inserted rows are visible.

### 7.2 Manual (not CI)

- `./scripts/bench-pg-tps.sh` against local PG — fill `docs/perf-baselines/p-005-pg-tps.md` with real numbers.
- `huggingface-cli repo info sentence-transformers/all-MiniLM-L6-v2 --field sha` — capture the alpha-demo SHA into the catalog.
- (Optional) `dlw-seed --demo` followed by `pnpm dev` browser smoke — alpha demo task should appear in TaskList.

### 7.3 No new CI jobs

Existing `pytest` job picks up new tests automatically. Existing `shellcheck` job will start scanning `scripts/` once that directory becomes non-empty — see §9 below.

---

## 8. File Structure

After this plan:

```
modelpull/
├── src/dlw/
│   ├── fixtures.py                                NEW
│   └── cli/
│       ├── __init__.py                            NEW (empty)
│       └── seed.py                                NEW
├── tests/
│   ├── test_fixtures.py                           NEW
│   └── cli/
│       ├── __init__.py                            NEW (empty)
│       └── test_seed_cli.py                       NEW
│   # tests/conftest.py NOT modified (see §3.3 — defer refactor to Phase 1 W6)
├── scripts/
│   └── bench-pg-tps.sh                            NEW
├── docs/
│   ├── perf-baselines/
│   │   ├── .gitignore                             NEW (excludes p-005-pg-tps.txt)
│   │   └── p-005-pg-tps.md                        NEW
│   └── demo/
│       └── dataset-catalog.md                     NEW
├── pyproject.toml                                 MODIFY +dlw-seed entry-point
└── docs/superpowers/specs/2026-05-12-phase-1-week-5-dev-infra-design.md  (this file)
```

---

## 9. CI Considerations

`.github/workflows/ci.yml` `shellcheck` job currently scans `./deploy/runbooks/scripts`. Once `scripts/bench-pg-tps.sh` lands, shellcheck **should** also scan `./scripts`. PR #6 (alpha demo, `scripts/demo-alpha.sh`) is the first to introduce that directory.

Decision: do NOT change the shellcheck `scandir` in this plan. The script is `set -euo pipefail` + standard idioms; explicit `# shellcheck disable=...` not needed. Whoever merges first (PR #6 or this plan's PR) leaves shellcheck reading the old `scandir`; subsequent PR can extend it. This avoids needing to coordinate the in-flight PRs.

If PR #6 lands first and updates shellcheck scandir, this plan inherits it; if not, the script is unchecked by CI but is small + idiomatic.

---

## 10. Acceptance Criteria

- [ ] `dlw-seed --default` against fresh DB inserts 4 rows; second invocation no-ops.
- [ ] `dlw-seed --demo` on top of `--default` creates 1 pending `DownloadTask` row with `repo_id="sentence-transformers/all-MiniLM-L6-v2"`.
- [ ] 7 pytest tests pass; existing Phase 1 backend test suite (99 + 1 deselected, since this plan branches off main `c952957`) still green; 0 regressions.
- [ ] No existing test fixtures modified (per §3.3 decision); only NEW test files use `seed_default()`.
- [ ] `scripts/bench-pg-tps.sh` runs successfully on local PG 18:5433; produces `docs/perf-baselines/p-005-pg-tps.txt` raw + filled-in `.md` report with actual TPS number.
- [ ] `docs/demo/dataset-catalog.md` committed with 3 entries, alpha-demo model's revision SHA pinned (not `main`).
- [ ] CI 12/12 green on the PR (no CI changes; new tests auto-collected).
- [ ] No diff outside the File Structure list.
- [ ] No conflicts with PR #6 (alpha demo) or PR #7 (P2-W1 fence) — verified by `git diff main` showing only the listed file paths.

---

## 11. Implementation Phasing (preview for plan)

| Milestone | Deliverable | Verification |
|-----------|-------------|--------------|
| M1 fixtures core | `src/dlw/fixtures.py` + 4 unit tests | pytest tests/test_fixtures.py |
| M2 dlw-seed CLI | `src/dlw/cli/seed.py` + entry-point + 3 subprocess tests + `tests/conftest.py` reuse | pytest tests/ + `uv run dlw-seed --default` live |
| M3 P-005 bench | `scripts/bench-pg-tps.sh` + `docs/perf-baselines/p-005-pg-tps.md` + filled-in `.txt` from a real run | `bash -n` + actual `./scripts/bench-pg-tps.sh` against local PG |
| M4 demo catalog | `docs/demo/dataset-catalog.md` with pinned SHAs | `huggingface-cli repo info … --field sha` for each entry |
| M5 PR | push + open PR + monitor CI 12/12 | CI green |

Plan task count estimate: **~7–9 tasks**. Smaller than P2-W1's 16 — appropriate for slim scope.

---

## 12. References

- Roadmap source: `docs/v2.0/08-mvp-roadmap.md` §1.6 Week 5
- Target spec: `docs/v2.0/07-test-plan.md` §perf row P-005
- FEAS-03 operator doc (deferred CLI consumer): `docs/operator/onboard-first-executor.md`
- FEAS-04 operator doc (deferred CLI consumer): `docs/operator/oidc-setup.md`
- Phase 2 W2 entry criterion that consumes P-005 data: `docs/v2.0/08-mvp-roadmap.md` §2.4
- Existing test seed pattern to replace: `tests/conftest.py` `env` fixture (Phase 1 W2 introduced)
- Existing CLI entry-point pattern to copy: `src/dlw/executor/cli.py` + `pyproject.toml [project.scripts] dlw-executor`
- Alpha demo consumer: `scripts/demo-alpha.sh` (PR #6) + `tests/e2e/test_hf_s3_smoke_local.py` (PR #5 manual smoke)
