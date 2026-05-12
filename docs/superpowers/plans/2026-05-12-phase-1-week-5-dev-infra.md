# Phase 1 Week 5: Dev Infra (slim) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close three Phase-1-relevant dev-infra gaps: (1) a reusable `dlw.fixtures` seed module + `dlw-seed` CLI; (2) a P-005 PG TPS baseline produced via pgbench + report; (3) a pinned demo-dataset catalog. Zero schema changes, no new runtime deps, ~7 new pytest cases on top of Phase 1's 99 (1 deselected).

**Architecture:** `src/dlw/fixtures.py` exports `seed_default()` (4-row idempotent seed via PG `ON CONFLICT DO NOTHING`) + `seed_demo_data()` (adds a pending DownloadTask). `src/dlw/cli/seed.py` is the `dlw-seed` argparse CLI registered as a pyproject entry-point (mirrors W3's `dlw-executor` pattern). `scripts/bench-pg-tps.sh` wraps `pgbench` and tees output into a gitignored raw file; the operator hand-curates `docs/perf-baselines/p-005-pg-tps.md` with results + interpretation. `docs/demo/dataset-catalog.md` pins HuggingFace revision SHAs for three referenced models.

**Tech Stack:** Plain Python + SQLAlchemy 2.x async (no new deps). pgbench from OS-level `postgresql-client`. `argparse` + `asyncio.run` (same as `dlw-executor`). Tests use `pytest` + `subprocess.run` for CLI smoke (same as W3's `tests/executor/test_cli.py`).

**Scope:** 3 deliverables, 6 implementation tasks across 4 milestones. **Branch off `main` at `c952957`** (PR #5 merge). PR #6 (alpha demo, `feat/phase-1-alpha-demo`) and PR #7 (P2-W1 fence, `feat/phase-2-w1-fence-token`) are in flight — this plan is orthogonal to both (no file overlap; no shared CI gate). Companion spec: `docs/superpowers/specs/2026-05-12-phase-1-week-5-dev-infra-design.md`.

**Pre-flight:** Phase 1 W1-W4 merged (`main` = `c952957`). Branch `feat/phase-1-week-5-dev-infra` exists with spec committed (commit `78bfa63`). Local PG running on `localhost:5433`. `uv` 0.11.9. PostgreSQL client tools (`pgbench`, `createdb`, `dropdb`, `psql`) in PATH. `huggingface-cli` available (transitive of `huggingface_hub` already in deps; run `uv run huggingface-cli --help`).

**Out-of-scope (deferred — explicit list per spec §1.2):**

- Self-hosted CI runner setup → Phase 2 W2 (chunk-level benchmark prerequisites)
- `dlw admin executor-token` CLI (FEAS-03) → Phase 2 W3 (mTLS bootstrap surface)
- `dlw admin` multi-user CLI (FEAS-04) → Phase 3 (OIDC + multi-tenant)
- OIDC IdP setup docs → already shipped in `docs/operator/oidc-setup.md` (v2.0.13)
- Multi-model runs against real HF → Phase 2 ChecksumSHA256 validation
- pgbench business-workload script → Phase 2 W2 multi-executor scheduler benchmark
- P-001 / P-002 / P-003 / P-004 baselines → later perf gates (different tooling, different acceptance)
- `tests/conftest.py` `env` fixture refactor → Phase 1 W6 cleanup (avoids conflict with PR #6 / #7 in flight)

---

## File Structure

After this plan:

```
modelpull/
├── src/dlw/
│   ├── fixtures.py                                NEW (seed_default + seed_demo_data + DemoModel constants)
│   └── cli/
│       ├── __init__.py                            NEW (empty)
│       └── seed.py                                NEW (dlw-seed CLI)
├── tests/
│   ├── test_fixtures.py                           NEW (4 tests)
│   └── cli/
│       ├── __init__.py                            NEW (empty)
│       └── test_seed_cli.py                       NEW (3 subprocess tests)
├── scripts/
│   └── bench-pg-tps.sh                            NEW
├── docs/
│   ├── perf-baselines/
│   │   ├── .gitignore                             NEW (raw `*.txt` excluded)
│   │   └── p-005-pg-tps.md                        NEW (Result section filled in M3 after live run)
│   └── demo/
│       └── dataset-catalog.md                     NEW (3 entries with pinned SHAs)
├── pyproject.toml                                 MODIFY (+dlw-seed entry-point)
└── docs/superpowers/specs/2026-05-12-phase-1-week-5-dev-infra-design.md  (already committed)
```

**Why this structure:** each file has one responsibility. `dlw.fixtures` is a service-layer helper (no HTTP, no CLI args); `dlw.cli.seed` is a thin argparse + asyncio wrapper. `scripts/` and `docs/perf-baselines/` are new directories; `scripts/` is also touched by in-flight PR #6 (`scripts/demo-alpha.sh`) but no file collision.

---

## Pre-flight checks

- [ ] On branch `feat/phase-1-week-5-dev-infra`, spec committed (`git log --oneline -1` shows `78bfa63` or descendant).
- [ ] `main` at `c952957` (PR #5 merge): `git log main --oneline -1`.
- [ ] PG running on `localhost:5433` (`pg_isready -h localhost -p 5433`).
- [ ] `dlw` database exists with Phase 1 W4 schema (`uv run alembic upgrade head` is a no-op).
- [ ] Existing pytest suite green (`uv run pytest -x` → 99 passed, 1 deselected).
- [ ] `uv --version` ≥ 0.11.9.
- [ ] `pgbench --version` works (any 14+ should match PG 18 wire protocol).
- [ ] `psql -h localhost -p 5433 -U postgres -c "SELECT 1"` returns 1.

---

## Milestone 1 — Fixtures core + CLI

After M1, `dlw-seed --default` and `dlw-seed --demo` work; new pytest tests cover both the module and the CLI.

---

### Task 1: `dlw.fixtures` module + 4 unit tests

**Files:**
- Create: `src/dlw/fixtures.py`
- Create: `tests/test_fixtures.py`

- [ ] **Step 1: Write failing tests `tests/test_fixtures.py`**

```python
"""Tests for dlw.fixtures seed module (Phase 1 W5)."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.base import Base
from dlw.db.models.storage import StorageBackend
from dlw.db.models.task import DownloadTask
from dlw.db.models.tenant import Project, Tenant, User
from dlw.fixtures import StorageSeed, seed_default, seed_demo_data


@pytest.fixture(scope="module", autouse=True)
async def _create_tables(engine):
    """Create schema for the module; drop at module end (Phase 1 W5 W6-M discipline)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.mark.slow
async def test_seed_default_inserts_four_rows(db_session: AsyncSession) -> None:
    """seed_default creates exactly 1 tenant + 1 project + 1 user + 1 storage."""
    await seed_default(db_session)
    await db_session.flush()

    assert await db_session.scalar(select(func.count()).select_from(Tenant)) == 1
    assert await db_session.scalar(select(func.count()).select_from(Project)) == 1
    assert await db_session.scalar(select(func.count()).select_from(User)) == 1
    assert await db_session.scalar(select(func.count()).select_from(StorageBackend)) == 1


@pytest.mark.slow
async def test_seed_default_idempotent_via_on_conflict(
    db_session: AsyncSession,
) -> None:
    """Calling seed_default twice is a no-op (ON CONFLICT DO NOTHING)."""
    await seed_default(db_session)
    await db_session.flush()
    await seed_default(db_session)        # second call must not error
    await db_session.flush()
    # Still exactly 1 row per table
    assert await db_session.scalar(select(func.count()).select_from(Tenant)) == 1
    assert await db_session.scalar(select(func.count()).select_from(StorageBackend)) == 1


@pytest.mark.slow
async def test_seed_default_with_custom_storage_config(
    db_session: AsyncSession,
) -> None:
    """StorageSeed(config={...}) → config_encrypted contains JSON bytes."""
    cfg = {"bucket": "test-bucket", "region": "us-east-1",
           "endpoint_url": "http://minio:9000", "key_prefix": "p/"}
    await seed_default(db_session, storage=StorageSeed(config=cfg))
    await db_session.flush()

    sb = await db_session.get(StorageBackend, 1)
    assert sb is not None
    assert json.loads(bytes(sb.config_encrypted).decode("utf-8")) == cfg


@pytest.mark.slow
async def test_seed_demo_data_creates_pending_task(
    db_session: AsyncSession,
) -> None:
    """seed_demo_data adds 1 DownloadTask with status='pending' pointing to demo model."""
    await seed_demo_data(db_session)
    await db_session.flush()

    tasks = (await db_session.execute(select(DownloadTask))).scalars().all()
    assert len(tasks) == 1
    t = tasks[0]
    assert t.status == "pending"
    assert t.repo_id == "sentence-transformers/all-MiniLM-L6-v2"
    # M3 will pin a real SHA; M1 ships a placeholder revision string
    assert t.revision != ""
    assert t.tenant_id == 1
```

- [ ] **Step 2: Run tests; expected FAIL**

```bash
uv run pytest tests/test_fixtures.py -v
```

Expected: 4 FAILs — `ModuleNotFoundError: No module named 'dlw.fixtures'`.

- [ ] **Step 3: Create `src/dlw/fixtures.py`**

```python
"""Reproducible seed data for dev / demo / tests.

Phase 1: single tenant + single user + single storage backend (id=1).
Phase 3 will expand multi-tenant; signature kept forward-compatible.

Caller commits — service-layer convention (matches scheduler / executor_service).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession


# Demo dataset alpha — pinned SHA filled in M5 (Task 5) alongside dataset-catalog.md.
# Placeholder during M1/M2 so seed_demo_data works against test DB; not a real revision.
ALPHA_DEMO_REPO_ID = "sentence-transformers/all-MiniLM-L6-v2"
ALPHA_DEMO_REVISION = "main"   # PLACEHOLDER — Task 5 replaces with pinned commit SHA


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
    config: dict[str, Any] | None = None   # None → b""; dict → JSON-serialised


async def seed_default(
    session: AsyncSession,
    *,
    tenant: TenantSeed = TenantSeed(),
    project: ProjectSeed = ProjectSeed(),
    user: UserSeed = UserSeed(),
    storage: StorageSeed = StorageSeed(),
) -> None:
    """Insert standard Phase 1 4-row seed (tenant/project/user/storage).

    Idempotent via PG ON CONFLICT (id) DO NOTHING — second invocation is a no-op.
    Caller commits.
    """
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Project, Tenant, User

    await session.execute(pg_insert(Tenant).values(
        id=tenant.id, slug=tenant.slug, display_name=tenant.display_name,
    ).on_conflict_do_nothing(index_elements=["id"]))

    await session.execute(pg_insert(Project).values(
        id=project.id, tenant_id=project.tenant_id, name=project.name,
    ).on_conflict_do_nothing(index_elements=["id"]))

    await session.execute(pg_insert(User).values(
        id=user.id, tenant_id=user.tenant_id, oidc_subject=user.oidc_subject,
        email=user.email, role=user.role,
    ).on_conflict_do_nothing(index_elements=["id"]))

    config_bytes = b"" if storage.config is None else (
        json.dumps(storage.config).encode("utf-8")
    )
    await session.execute(pg_insert(StorageBackend).values(
        id=storage.id, tenant_id=storage.tenant_id, name=storage.name,
        backend_type=storage.backend_type, region=storage.region,
        config_encrypted=config_bytes,
    ).on_conflict_do_nothing(index_elements=["id"]))


async def seed_demo_data(session: AsyncSession) -> None:
    """seed_default + 1 pending DownloadTask pointing at alpha demo model.

    Idempotent — task uses ON CONFLICT DO NOTHING on (tenant_id, repo_id, revision)
    via a synthetic uniqueness check; for Phase 1 simplicity we check existence
    first because DownloadTask.id is a generated UUID (no natural unique key).
    """
    from sqlalchemy import select
    from dlw.db.models.task import DownloadTask

    # Storage config pointing at local MinIO (matches docker-compose dev profile)
    demo_storage_cfg = {
        "bucket": "modelpull-dev",
        "region": "us-east-1",
        "endpoint_url": "http://localhost:9000",
        "key_prefix": "phase1/",
    }
    await seed_default(session, storage=StorageSeed(config=demo_storage_cfg))

    # Check existence to keep idempotent (DownloadTask has no natural unique key)
    existing = await session.scalar(
        select(DownloadTask.id)
        .where(DownloadTask.tenant_id == 1)
        .where(DownloadTask.repo_id == ALPHA_DEMO_REPO_ID)
        .where(DownloadTask.revision == ALPHA_DEMO_REVISION)
        .limit(1)
    )
    if existing is not None:
        return

    task = DownloadTask(
        tenant_id=1, project_id=1, owner_user_id=1,
        repo_id=ALPHA_DEMO_REPO_ID,
        revision=ALPHA_DEMO_REVISION,
        storage_id=1,
        path_template="{tenant}/{repo_id}/{revision}",
        priority=1,
        status="pending",
    )
    session.add(task)
```

- [ ] **Step 4: Run tests; expected PASS**

```bash
uv run pytest tests/test_fixtures.py -v
```

Expected: 4 PASS.

- [ ] **Step 5: Run full pytest to confirm no regression**

```bash
uv run pytest -x
```

Expected: 99 + 4 = 103 passed, 1 deselected. No existing tests touched.

- [ ] **Step 6: Commit**

```bash
git add src/dlw/fixtures.py tests/test_fixtures.py
git commit -m "feat(fixtures): seed_default + seed_demo_data + dataclass seeds (W5)"
```

---

### Task 2: `dlw-seed` CLI + 3 subprocess tests

**Files:**
- Create: `src/dlw/cli/__init__.py` (empty)
- Create: `src/dlw/cli/seed.py`
- Create: `tests/cli/__init__.py` (empty)
- Create: `tests/cli/test_seed_cli.py`
- Modify: `pyproject.toml` — `[project.scripts]` add `dlw-seed`

- [ ] **Step 1: Modify `pyproject.toml`**

In `[project.scripts]` (currently has only `dlw-executor`), append:

```toml
[project.scripts]
dlw-executor = "dlw.executor.cli:main"
dlw-seed = "dlw.cli.seed:main"
```

- [ ] **Step 2: `uv sync --all-groups` to register the new entry-point**

```bash
uv sync --all-groups
```

Expected: no new packages installed; entry-point linked into venv. Confirm:

```bash
uv run dlw-seed --help
```

Expected: error like `No module named 'dlw.cli'` (we haven't created it yet — that's the next step). Or — depending on pip's resolution — entry-point exists but fails at import. Either is fine; Step 4 fixes it.

- [ ] **Step 3: Create empty package files**

```bash
mkdir -p src/dlw/cli tests/cli
touch src/dlw/cli/__init__.py tests/cli/__init__.py
```

- [ ] **Step 4: Write failing tests `tests/cli/test_seed_cli.py`**

```python
"""Tests for dlw-seed CLI — subprocess against the per-session test DB."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.base import Base
from dlw.db.models.storage import StorageBackend
from dlw.db.models.task import DownloadTask


def _env_for_subprocess(test_db_name: str) -> dict[str, str]:
    """Build env var dict pointing CLI at the per-session test DB."""
    return {
        **{k: v for k, v in os.environ.items() if not k.startswith("DLW_DB_")},
        "DLW_DB_HOST": os.environ.get("DLW_TEST_PG_HOST", "localhost"),
        "DLW_DB_PORT": os.environ.get("DLW_TEST_PG_PORT", "5433"),
        "DLW_DB_USER": os.environ.get("DLW_TEST_PG_USER", "postgres"),
        "DLW_DB_PASSWORD": os.environ.get("DLW_TEST_PG_PASSWORD", ""),
        "DLW_DB_NAME": test_db_name,
        "DLW_BEARER_TOKEN": "ignored-by-seed",   # CLI doesn't use it but Settings validates
    }


@pytest.fixture(scope="module", autouse=True)
async def _create_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.mark.slow
def test_seed_cli_no_arg_errors() -> None:
    """Without --default or --demo, argparse must exit 2 with a usage message."""
    r = subprocess.run(
        [sys.executable, "-m", "dlw.cli.seed"],
        capture_output=True, text=True, timeout=15,
    )
    assert r.returncode == 2
    combined = r.stdout + r.stderr
    assert "default" in combined or "demo" in combined


@pytest.mark.slow
async def test_seed_cli_default_runs(
    test_db_name: str, db_session: AsyncSession,
) -> None:
    """`dlw-seed --default` against test DB inserts 4 rows."""
    r = subprocess.run(
        [sys.executable, "-m", "dlw.cli.seed", "--default"],
        capture_output=True, text=True, timeout=30,
        env=_env_for_subprocess(test_db_name),
    )
    assert r.returncode == 0, r.stderr
    assert "default seed applied" in (r.stdout + r.stderr).lower()

    # Verify via direct session
    count = await db_session.scalar(select(func.count()).select_from(StorageBackend))
    assert count == 1


@pytest.mark.slow
async def test_seed_cli_demo_runs(
    test_db_name: str, db_session: AsyncSession,
) -> None:
    """`dlw-seed --demo` creates the demo DownloadTask."""
    r = subprocess.run(
        [sys.executable, "-m", "dlw.cli.seed", "--demo"],
        capture_output=True, text=True, timeout=30,
        env=_env_for_subprocess(test_db_name),
    )
    assert r.returncode == 0, r.stderr
    assert "demo seed applied" in (r.stdout + r.stderr).lower()

    # The demo task should exist
    task = (await db_session.execute(
        select(DownloadTask).where(DownloadTask.repo_id == "sentence-transformers/all-MiniLM-L6-v2")
    )).scalar_one_or_none()
    assert task is not None
    assert task.status == "pending"
```

- [ ] **Step 5: Run; expected FAIL**

```bash
uv run pytest tests/cli/test_seed_cli.py -v
```

Expected: 3 FAILs — `ModuleNotFoundError: No module named 'dlw.cli.seed'`.

- [ ] **Step 6: Create `src/dlw/cli/seed.py`**

```python
"""dlw-seed CLI — insert reproducible dev/demo data into the controller DB.

Usage:
  dlw-seed --default     # 4-row Phase 1 default seed (idempotent)
  dlw-seed --demo        # seed_default + 1 pending DownloadTask
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.db.session import get_engine, reset_engine
from dlw.fixtures import seed_default, seed_demo_data

logger = logging.getLogger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="dlw-seed",
        description="Insert reproducible dev/demo data into the modelpull DB.",
    )
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument(
        "--default",
        action="store_true",
        help="Insert the 4-row Phase 1 default seed (idempotent)",
    )
    grp.add_argument(
        "--demo",
        action="store_true",
        help="seed_default + 1 demo task pointing to alpha demo model (idempotent)",
    )
    return p.parse_args(argv)


async def _async_main(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    try:
        async with factory() as session:
            if args.default:
                await seed_default(session)
                action = "default seed"
            else:  # args.demo
                await seed_demo_data(session)
                action = "demo seed"
            await session.commit()
        print(f"[dlw-seed] {action} applied.")
        return 0
    finally:
        await reset_engine()


def main() -> int:
    args = _parse_args()
    try:
        return asyncio.run(_async_main(args))
    except Exception as e:
        logger.exception("dlw-seed failed: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 7: Run tests; expected PASS**

```bash
uv run pytest tests/cli/test_seed_cli.py -v
```

Expected: 3 PASS. If the CLI tests time out, increase `timeout=30` to `timeout=60`; first subprocess invocation may pay uv resolver cost.

- [ ] **Step 8: Run full pytest**

```bash
uv run pytest -x
```

Expected: 103 + 3 = 106 passed, 1 deselected.

- [ ] **Step 9: Live smoke (manual; not a pytest)**

```bash
DLW_DB_HOST=localhost DLW_DB_PORT=5433 DLW_DB_USER=postgres \
DLW_DB_NAME=dlw DLW_BEARER_TOKEN=ignored \
uv run dlw-seed --default
```

Expected output:
```
[dlw-seed] default seed applied.
```

Second invocation prints the same line (idempotent — no error).

```bash
psql -h localhost -p 5433 -U postgres -d dlw -c "SELECT count(*) FROM tenants WHERE id=1"
```

Expected: `1`.

- [ ] **Step 10: Commit**

```bash
git add pyproject.toml src/dlw/cli/__init__.py src/dlw/cli/seed.py tests/cli/__init__.py tests/cli/test_seed_cli.py uv.lock
git commit -m "feat(cli): dlw-seed — argparse + asyncio + pyproject entry-point (W5)"
```

(Note: `uv.lock` may be unchanged since no deps were added; include it if `git status` shows it modified.)

### Milestone 1 verification (self)

```bash
uv run pytest -x        # 106 passed, 1 deselected
uv run dlw-seed --help  # prints argparse usage
uv run dlw-seed --default
uv run dlw-seed --demo
psql -h localhost -p 5433 -U postgres -d dlw -c "SELECT repo_id, status FROM download_tasks WHERE repo_id LIKE 'sentence-%' LIMIT 1"
```

Expected: pytest green; demo task row visible.

---

## Milestone 2 — P-005 PG TPS bench script

After M2, `./scripts/bench-pg-tps.sh` runs cleanly; the `.md` report skeleton is committed. M3 fills in real numbers.

---

### Task 3: `scripts/bench-pg-tps.sh` + `.md` report skeleton + gitignore

**Files:**
- Create: `scripts/bench-pg-tps.sh` (executable)
- Create: `docs/perf-baselines/.gitignore`
- Create: `docs/perf-baselines/p-005-pg-tps.md`

- [ ] **Step 1: Create `scripts/bench-pg-tps.sh`**

```bash
#!/usr/bin/env bash
# scripts/bench-pg-tps.sh — P-005 baseline: PostgreSQL transaction throughput.
#
# Per docs/v2.0/07-test-plan.md §perf P-005: target 5000 commits/s.
# Phase 1 produces a baseline on local PG; Phase 2 W2 entry criterion (§2.4)
# says: "Phase 1 实测 P-005 数据存在；如不达标先优化".
#
# Usage:
#   ./scripts/bench-pg-tps.sh
#   PGBENCH_SECONDS=120 PGBENCH_CLIENTS=20 ./scripts/bench-pg-tps.sh
#
# Prerequisites:
#   - postgresql-client (pgbench + createdb + dropdb + psql) in PATH
#   - DB on localhost:5433 with trust auth as user `postgres` (Phase 1 dev convention)
#
# Output:
#   - stdout: tee'd to docs/perf-baselines/p-005-pg-tps.txt (gitignored raw)
#   - operator hand-fills docs/perf-baselines/p-005-pg-tps.md with interpretation

set -euo pipefail

PG_HOST="${PGHOST:-localhost}"
PG_PORT="${PGPORT:-5433}"
PG_USER="${PGUSER:-postgres}"
BENCH_DB="${PGBENCH_DB:-pgbench_p005}"
BENCH_SCALE="${PGBENCH_SCALE:-10}"
BENCH_SECONDS="${PGBENCH_SECONDS:-60}"
BENCH_CLIENTS="${PGBENCH_CLIENTS:-10}"
BENCH_JOBS="${PGBENCH_JOBS:-4}"
OUT_DIR="${OUT_DIR:-docs/perf-baselines}"
OUT_TXT="$OUT_DIR/p-005-pg-tps.txt"

if ! command -v pgbench >/dev/null 2>&1; then
  echo "ERROR: pgbench not in PATH."
  echo "Install: brew install postgresql / apt install postgresql-contrib / similar"
  exit 2
fi

mkdir -p "$OUT_DIR"

echo "[1/4] Drop + recreate bench DB ($BENCH_DB) ..."
dropdb -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" --if-exists "$BENCH_DB"
createdb -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$BENCH_DB"

echo "[2/4] Initialize bench schema (scale=$BENCH_SCALE) ..."
pgbench -i -s "$BENCH_SCALE" -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$BENCH_DB"

echo "[3/4] Run TPS test ($BENCH_CLIENTS clients x $BENCH_JOBS jobs x ${BENCH_SECONDS}s) ..."
{
  echo "=== P-005 PG TPS baseline ==="
  echo "Date:        $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "Host:        $(uname -a)"
  echo "PG version:  $(psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$BENCH_DB" -t -c 'SHOW server_version' | xargs)"
  echo "DB scale:    $BENCH_SCALE"
  echo "Clients:     $BENCH_CLIENTS"
  echo "Jobs:        $BENCH_JOBS"
  echo "Duration:    ${BENCH_SECONDS}s"
  echo "Target:      5000 commits/s (07-test-plan §P-005)"
  echo "---"
  pgbench -c "$BENCH_CLIENTS" -j "$BENCH_JOBS" -T "$BENCH_SECONDS" \
    --progress=10 \
    -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$BENCH_DB"
} | tee "$OUT_TXT"

echo "[4/4] Result raw saved -> $OUT_TXT"
echo
echo "Next: review $OUT_TXT, then update docs/perf-baselines/p-005-pg-tps.md"
echo "      Result + Environment sections with the actual numbers."
```

Make it executable:

```bash
chmod +x scripts/bench-pg-tps.sh
```

- [ ] **Step 2: Create `docs/perf-baselines/.gitignore`**

```
# Raw pgbench output (machine-specific; do not commit)
p-005-pg-tps.txt
```

- [ ] **Step 3: Create `docs/perf-baselines/p-005-pg-tps.md` skeleton**

```markdown
# P-005 — PostgreSQL TPS Baseline

> Per `docs/v2.0/07-test-plan.md` §perf row P-005.
> Target: **5000 commits/s** sustained.
> Phase 2 W2 §2.4 entry: "Phase 1 实测 P-005 数据存在；如不达标先优化".

## Methodology

`pgbench` TPC-B-like workload on a freshly initialised DB (scale 10 ≈ 150 MB).
Run via `./scripts/bench-pg-tps.sh` — see that script for parameters + raw
output capture.

## Environment

| Field | Value |
|-------|-------|
| Date | _filled by `scripts/bench-pg-tps.sh` Step 4 / Task 4_ |
| Host | _filled — `uname -a` from script header_ |
| OS | _Windows 11 / Linux / macOS — filled at run time_ |
| CPU | _filled manually_ |
| Memory | _filled manually_ |
| Disk | _filled manually (NVMe / SATA SSD / HDD)_ |
| PG version | _filled by script (`SHOW server_version`)_ |
| PG auth | trust (dev only) |
| PG fsync / synchronous_commit | default (on / on) |

## Result

_M3 (Task 4) fills this section with the tail of `docs/perf-baselines/p-005-pg-tps.txt`:_

```
<sustained TPS line(s) from pgbench output, e.g.:
  tps = 2473.41 (without initial connection time)
  tps = 2475.83 (without initial connection time)>
```

**Sustained TPS: _filled by Task 4_ commits/s**

## Assessment vs target (5000 commits/s)

| Outcome | Action |
|---------|--------|
| ≥ 5000 | ✅ Phase 2 W2 entry met. No optimisation needed. |
| 1000–5000 | ⚠️ Marginal. Likely usable for Phase 2 single-controller — re-run with production-grade PG (managed RDS / fsync tuning) before declaring P-005 failed. Document under Phase 2 W2 entry §2.4. |
| < 1000 | ❌ Optimise first. Investigate: fsync, WAL, disk, max_connections. |

_M3 (Task 4) fills the verdict here based on actual numbers._

## What this does NOT measure

- modelpull's actual write mix (`claim_one_subtask FOR UPDATE SKIP LOCKED` + `complete_subtask`). Phase 2 W2 may add a custom `pgbench --file` script if business-workload TPS diverges from TPC-B.
- Multi-executor concurrency. Phase 2 W2's multi-executor scheduler benchmark covers this.
- Read-heavy load (Phase 1 W3 UI polling `/tasks` every 5s). P-002 covers controller API QPS.

## Reproduce

```bash
cd <repo root>
./scripts/bench-pg-tps.sh
# → tees raw output to docs/perf-baselines/p-005-pg-tps.txt (gitignored)
# → then update Environment + Result + Assessment sections in this file
```
```

- [ ] **Step 4: Validate bash syntax**

```bash
bash -n scripts/bench-pg-tps.sh
```

Expected: no output (silent success).

- [ ] **Step 5: Commit**

```bash
git add scripts/bench-pg-tps.sh docs/perf-baselines/.gitignore docs/perf-baselines/p-005-pg-tps.md
git commit -m "feat(perf): scripts/bench-pg-tps.sh + P-005 report skeleton (W5)"
```

---

## Milestone 3 — P-005 baseline live run

After M3, the `.md` report has real numbers from the local PG. This milestone is a **manual self-task**, not a subagent dispatch — the operator runs the script and interprets results.

---

### Task 4: Live pgbench run + fill `.md` report

**Files:**
- Modify: `docs/perf-baselines/p-005-pg-tps.md`

This task is self-executed (not dispatchable to a subagent because it depends on machine state).

- [ ] **Step 1: Run the bench script**

```bash
./scripts/bench-pg-tps.sh
```

Expected: ~70-90s total wall time (60s test + setup). Final lines show:

```
tps = NNNN.NN (without initial connection time)
tps = NNNN.NN (without initial connection time)
```

- [ ] **Step 2: Read the raw output**

```bash
cat docs/perf-baselines/p-005-pg-tps.txt | tail -30
```

Locate the two `tps =` lines plus the header showing Host / PG version / etc.

- [ ] **Step 3: Update `docs/perf-baselines/p-005-pg-tps.md`**

Edit the three sections:

**Environment** — fill the table from script header (Date, Host) + manual lookup (CPU, Memory, Disk). On Windows, run `wmic cpu get name` for CPU and check Settings → System → About for memory.

**Result** — paste the relevant pgbench tail lines into the code block; fill the bold `Sustained TPS:` value with the "without initial connection time" number.

**Assessment** — pick the matching verdict bucket (≥5000 / 1000-5000 / <1000) and write 1-3 sentences justifying it. If marginal (the most likely outcome on local dev PG), note that Phase 2 W2 entry §2.4 acknowledges Phase 1 numbers as informational; production PG (managed / tuned) will be re-tested as part of Phase 2 W2.

- [ ] **Step 4: Verify `.md` renders cleanly**

```bash
uv run python -c "import pathlib; print(pathlib.Path('docs/perf-baselines/p-005-pg-tps.md').read_text(encoding='utf-8')[:500])"
```

Expected: shows the updated content with no broken markdown.

- [ ] **Step 5: Commit**

```bash
git add docs/perf-baselines/p-005-pg-tps.md
git commit -m "perf(baseline): P-005 PG TPS measured on local PG 18:5433 (W5)"
```

---

## Milestone 4 — Demo dataset catalog

After M4, `docs/demo/dataset-catalog.md` lists 3 HuggingFace models with revision SHAs pinned. `dlw.fixtures.ALPHA_DEMO_REVISION` is updated from `"main"` placeholder to the actual SHA.

---

### Task 5: Pin HuggingFace SHAs + write catalog + update fixtures

**Files:**
- Create: `docs/demo/dataset-catalog.md`
- Modify: `src/dlw/fixtures.py` — replace `ALPHA_DEMO_REVISION = "main"` with pinned SHA

- [ ] **Step 1: Pin the alpha demo model's SHA**

```bash
uv run huggingface-cli scan-cache --help >/dev/null 2>&1 || true   # sanity: HF SDK installed
uv run python -c "
from huggingface_hub import HfApi
info = HfApi().repo_info('sentence-transformers/all-MiniLM-L6-v2', revision='main')
print(info.sha)
"
```

Expected: a 40-char hex SHA (e.g. `c9745ed1d9f207416be6d2e6f8de32d1f16199bf`). Copy this value.

If the command fails (no network), fall back to:

```bash
curl -s https://huggingface.co/api/models/sentence-transformers/all-MiniLM-L6-v2/revision/main \
  | python -c "import sys,json; print(json.load(sys.stdin)['sha'])"
```

Record the SHA in a shell variable for the next steps:

```bash
ALPHA_SHA="<paste-the-40-char-hex-here>"
echo "alpha demo SHA: $ALPHA_SHA"
```

- [ ] **Step 2: Pin the faster-smoke model's SHA**

```bash
uv run python -c "
from huggingface_hub import HfApi
info = HfApi().repo_info('prajjwal1/bert-tiny', revision='main')
print(info.sha)
"
```

Record as `BERT_TINY_SHA`.

- [ ] **Step 3: Create `docs/demo/dataset-catalog.md`**

Replace the two `_PIN-AT-M4_` placeholders with the actual SHAs captured in Steps 1–2.

```markdown
# Demo Dataset Catalog

> Curated list of public HuggingFace models used by modelpull for demo,
> regression smoke, and (future) chunk-level benchmarks. All revisions are
> pinned to specific commit SHAs to keep CI / docs reproducible.

## 1. Phase 1 alpha demo (default)

| Field | Value |
|-------|-------|
| `repo_id` | `sentence-transformers/all-MiniLM-L6-v2` |
| `revision` | `_PIN-AT-M4_` <!-- replace with $ALPHA_SHA --> |
| Total size | ~91 MB |
| File count | 9 (config.json, model.safetensors, tokenizer files, pytorch_model.bin, …) |
| LFS files | Yes (model.safetensors + pytorch_model.bin) |
| License | Apache-2.0 |
| Use cases | `scripts/demo-alpha.sh` default (PR #6); manual smoke `tests/e2e/test_hf_s3_smoke_local.py` (PR #5); `dlw-seed --demo` |
| Walltime estimate | 30–90 s on 100 Mbps link |

Pin command (used during W5 implementation):

\`\`\`bash
uv run python -c "from huggingface_hub import HfApi; print(HfApi().repo_info('sentence-transformers/all-MiniLM-L6-v2', revision='main').sha)"
\`\`\`

## 2. Faster smoke (ad-hoc dev iteration)

| Field | Value |
|-------|-------|
| `repo_id` | `prajjwal1/bert-tiny` |
| `revision` | `_PIN-AT-M4_` <!-- replace with $BERT_TINY_SHA --> |
| Total size | ~17 MB |
| Use cases | Fast iteration on the executor pipeline; not used by default demo |

## 3. Phase 2+ (chunk-level / large-file path)

NOT downloaded in Phase 1 CI or alpha demo. Catalogued for forward planning.

| Field | Value |
|-------|-------|
| `repo_id` | `deepseek-ai/DeepSeek-V3` |
| Total size | ~689 GB / 163 files (FP8) |
| Use cases | Phase 2 W2 chunk-level multi-thread benchmark; Phase 2 §2.5 P-004 1 GB/s target |

## How to add a new entry

1. Pin the revision SHA (see Pin command above); never use `main` for catalog entries — it drifts.
2. Note license; modelpull alpha is internal but downstream redistribution implications matter.
3. If the model is gated/private, add an "Auth required" row + cross-link to Phase 2 plan (HF Token reverse-proxy).
4. Update this file in the same PR as any test / script that depends on the new entry.

## References

- Phase 1 §1.5 acceptance E2E-001 — single model HF→S3 (any from #1 or #2 satisfies)
- `scripts/demo-alpha.sh` — uses entry #1 by default; override via `DEMO_REPO_ID` env (PR #6)
- `src/dlw/fixtures.py` `ALPHA_DEMO_REPO_ID` / `ALPHA_DEMO_REVISION` — used by `dlw-seed --demo`
- `docs/demo/runbook.md` — operator-facing demo flow (PR #6)
```

After writing the file, manually replace both `_PIN-AT-M4_` placeholders with the actual SHA strings captured in Steps 1–2 (use editor find/replace or `sed -i`).

- [ ] **Step 4: Update `src/dlw/fixtures.py` `ALPHA_DEMO_REVISION`**

Find this line in `src/dlw/fixtures.py`:

```python
ALPHA_DEMO_REVISION = "main"   # PLACEHOLDER — Task 5 replaces with pinned commit SHA
```

Replace with:

```python
ALPHA_DEMO_REVISION = "<paste $ALPHA_SHA here>"   # pinned per docs/demo/dataset-catalog.md
```

(Use the same `$ALPHA_SHA` value from Step 1.)

- [ ] **Step 5: Run pytest to confirm fixtures still pass with the new revision**

```bash
uv run pytest tests/test_fixtures.py tests/cli/test_seed_cli.py -v
```

Expected: 7 PASS. `test_seed_demo_data_creates_pending_task` only asserts `t.revision != ""` — any non-empty string passes.

- [ ] **Step 6: Run full pytest**

```bash
uv run pytest -x
```

Expected: 106 passed, 1 deselected.

- [ ] **Step 7: Commit**

```bash
git add docs/demo/dataset-catalog.md src/dlw/fixtures.py
git commit -m "docs(demo): dataset-catalog + pin alpha demo revision SHA (W5)"
```

---

## Milestone 5 — Push + open PR

---

### Task 6: Push branch + open PR + monitor CI

- [ ] **Step 1: Confirm branch state**

```bash
git status              # clean working tree
git log main..HEAD --oneline | wc -l   # 6-8 commits (spec + 5 task commits)
```

- [ ] **Step 2: Push the branch**

```bash
git push -u origin feat/phase-1-week-5-dev-infra
```

- [ ] **Step 3: Open the PR**

```bash
gh pr create \
  --title "Phase 1 Week 5 — Dev Infra (slim): fixtures + dlw-seed CLI + P-005 baseline + dataset catalog" \
  --body "$(cat <<EOF
## Summary

Three orthogonal dev-infra deliverables, slim per spec §1.2 (CI runner / admin CLI / enrollment CLI deferred to Phase 2/3 where their prerequisites exist):

- \`src/dlw/fixtures.py\` — reusable seed (\`seed_default\` + \`seed_demo_data\`) via PG \`ON CONFLICT DO NOTHING\`; dataclass kwargs for forward-compat with Phase 3 multi-tenant.
- \`dlw-seed\` CLI — argparse + asyncio, mirrors W3's \`dlw-executor\` pattern; registered via pyproject \`[project.scripts]\`.
- \`scripts/bench-pg-tps.sh\` + \`docs/perf-baselines/p-005-pg-tps.md\` — wraps pgbench, raw output gitignored, human-curated interpretation committed. Phase 2 W2 §2.4 entry artefact.
- \`docs/demo/dataset-catalog.md\` — 3 HuggingFace entries with pinned revision SHAs (alpha demo + faster smoke + Phase 2+ catalogued).

Branch is orthogonal to PR #6 (alpha demo) and PR #7 (P2-W1 fence) — no file overlap.

## Test plan

- [x] Backend pytest: 99 + 7 new (4 fixtures + 3 CLI subprocess) = 106 passed, 1 deselected. Zero regressions.
- [x] \`./scripts/bench-pg-tps.sh\` runs on local PG 18:5433; produces raw \`.txt\` (gitignored) + filled-in \`.md\` report.
- [x] \`dlw-seed --default\` + \`dlw-seed --demo\` both succeed; idempotent on re-run.
- [x] \`docs/demo/dataset-catalog.md\` SHAs pinned (not \`main\`); \`src/dlw/fixtures.py ALPHA_DEMO_REVISION\` matches.
- [x] \`bash -n scripts/bench-pg-tps.sh\` syntax-clean.
- [x] No frontend / API / schema changes; \`pnpm typecheck/lint/test/build\` and \`alembic\` unaffected.

## Out of scope (deferred — see spec §1.2)

Self-hosted CI runner, \`dlw admin\` multi-user / enrollment CLI, OIDC bootstrap CLI, multi-model real-HF runs, pgbench business workload, other perf baselines — all moved to Phase 2/3 plans where their prerequisites are met.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Monitor CI**

```bash
gh pr checks $(gh pr view --json number -q .number) --watch
```

Expected: 12 checks pass. If `shellcheck` flags `scripts/bench-pg-tps.sh`, the most common issue is SC2086 (unquoted variables) — fix in a new commit; do NOT amend or force-push.

If `markdown` link-check flags broken references in `dataset-catalog.md` or `p-005-pg-tps.md`, the most common issue is heading anchors — fix in a new commit.

---

### Milestone 5 verification (self)

- [ ] PR opened; CI 12/12 green.
- [ ] All 106 backend tests pass locally and on CI.
- [ ] No regression on existing 99 tests.
- [ ] PR diff stays within the File Structure list (verify via `gh pr diff --name-only`).

---

## Definition of Done

- [ ] All 6 tasks committed on `feat/phase-1-week-5-dev-infra`.
- [ ] PR opened, CI 12/12 green.
- [ ] 7 new pytest tests pass; 99 existing tests still green.
- [ ] `scripts/bench-pg-tps.sh` runs cleanly on local PG; `.md` report committed with real numbers.
- [ ] `docs/demo/dataset-catalog.md` has pinned SHAs for both Phase-1-relevant entries (entry #3 is documented only — no SHA pin needed since it's Phase 2+ forward-planning).
- [ ] `src/dlw/fixtures.py ALPHA_DEMO_REVISION` matches catalog entry #1's revision.
- [ ] No diff outside the File Structure list (no existing fixtures modified per spec §3.3).
- [ ] `pyproject.toml` `[project.scripts]` registers both `dlw-executor` (existing) and `dlw-seed` (new).

---

## Plan Revisions Log

(Empty on first draft. Populated by the pre-execution multi-agent reviewer pass.)

| Tag | Severity | Issue | Fix applied |
|-----|----------|-------|-------------|
| _(none yet)_ | | | |

---

## References

- Spec: `docs/superpowers/specs/2026-05-12-phase-1-week-5-dev-infra-design.md`
- Roadmap source: `docs/v2.0/08-mvp-roadmap.md` §1.6 Week 5
- P-005 target spec: `docs/v2.0/07-test-plan.md` §perf row
- Existing CLI pattern (mirrored): `src/dlw/executor/cli.py` (W3)
- Existing CLI test pattern (mirrored): `tests/executor/test_cli.py` (W3)
- Existing conftest pattern: `tests/conftest.py` `engine` / `db_session` / `test_db_name` fixtures
- pyproject entry-points: `pyproject.toml` `[project.scripts]`
- HuggingFace SHA pin reference: `huggingface_hub.HfApi.repo_info(..., revision=...).sha`
