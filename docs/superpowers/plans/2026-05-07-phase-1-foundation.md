# Phase 1 Foundation: Controller Skeleton + v2.0 DB Schema Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** First executable code in modelpull. After this plan: `docker-compose up` followed by `curl localhost:8000/health/live` returns `{"status":"healthy"}`, all v2.0 Phase 1 tables exist in PG, and `pytest` shows all green.

**Architecture:** Single Python package `dlw` (controller in this plan; executor in Week 2's plan). Async SQLAlchemy 2.x + asyncpg + PG 16. Alembic migrations from greenfield (no v1.x baggage; migration content per `09-migration.md` §2.1). FastAPI app with lifespan-managed DB pool. pytest + testcontainers for per-test isolated PG.

**Tech Stack:** Python 3.12, uv (package manager), FastAPI 0.115+, SQLAlchemy 2.x async, asyncpg 0.29+, alembic 1.13+, pydantic-settings 2.x, structlog 24.x, pytest 8.x + pytest-asyncio 0.24 + testcontainers[postgres] 4.x, docker-compose.

**Scope:** Week 1 Day 1-5 of Phase 1 only (per `docs/v2.0/08-mvp-roadmap.md` §1.6). Subsequent weeks/phases need separate plans:

| Week | Sub-plan |
|------|----------|
| 1 | **This plan** — Foundation |
| 2 | Controller core (Task CRUD + scheduler loop CAS) |
| 3 | Executor process + UI scaffold |
| 4 | Verification (流式 sha256 + E2E-001) |
| 5 | Dev infra (FEAS-06 forgotten tasks) |
| 6 | Buffer + alpha demo |

This plan ends with: foundation ready for Week 2 to build CRUD + scheduler.

**Out-of-scope (NOT in this plan):**
- Authentication / OIDC (Week 2 simplified token; Phase 3 full OIDC)
- mTLS / fence token / executor (Week 2-3)
- Frontend (Week 3)
- Multi-tenancy logic (Phase 3); but `tenant_id` columns ARE in schema with hard-coded default = 1
- AI Copilot / multi-source / adaptive optimization (all v2.1+)

---

## File Structure

After this plan, the repo will contain:

```
modelpull/
├── pyproject.toml                                # uv-managed deps + ruff/mypy config
├── uv.lock                                       # auto-generated lockfile
├── .python-version                               # 3.12
├── docker-compose.dev.yml                        # PG 16 for dev
├── alembic.ini
├── src/dlw/
│   ├── __init__.py                               # version + package marker
│   ├── config.py                                 # pydantic-settings; env-driven
│   ├── logging_config.py                         # structlog + JSON output
│   ├── db/
│   │   ├── __init__.py
│   │   ├── base.py                               # SQLAlchemy declarative + naming convention
│   │   ├── session.py                            # async engine + session factory
│   │   └── models/
│   │       ├── __init__.py                       # re-exports all
│   │       ├── tenant.py                         # Tenant, Project, User
│   │       ├── storage.py                        # StorageBackend
│   │       ├── task.py                           # DownloadTask, FileSubTask
│   │       ├── executor.py                       # Executor, ExecutorStatusHistory
│   │       └── audit.py                          # AuditLog with chain_hash
│   ├── alembic/
│   │   ├── env.py                                # async-aware alembic env
│   │   ├── script.py.mako
│   │   └── versions/
│   │       └── 20260507_01_initial.py            # all v2.0 Phase 1 schema
│   ├── api/
│   │   ├── __init__.py
│   │   └── health.py                             # /health/live, /health/ready
│   └── main.py                                   # FastAPI app factory + lifespan
└── tests/
    ├── __init__.py
    ├── conftest.py                               # testcontainer PG; async session per test
    ├── db/
    │   ├── __init__.py
    │   ├── test_tenant.py
    │   ├── test_storage.py
    │   ├── test_task.py
    │   ├── test_executor.py
    │   ├── test_audit.py
    │   └── test_alembic.py                       # upgrade head + downgrade -1 + re-upgrade
    └── api/
        ├── __init__.py
        └── test_health.py
```

**Why this structure:** matches `docs/v2.0/01-architecture.md` §4 权威数据模型 grouping (identity / storage / task / executor / audit). Each model file owns ≤3 related tables; tests mirror by-domain. `dlw.alembic.env` is asyncio-aware (PG via asyncpg).

---

## Pre-flight checks (run once before starting)

- [ ] **Verify Python 3.12 installed**: `python3 --version` should show 3.12.x
- [ ] **Install uv**: `curl -LsSf https://astral.sh/uv/install.sh | sh` (or `pip install uv` for Windows)
- [ ] **Verify Docker installed**: `docker --version` shows 24.x+ and `docker compose version` shows v2.x
- [ ] **Verify git working dir clean**: `git status` shows no uncommitted changes (we'll commit per task)

---

## Task 1: Project skeleton (pyproject.toml + dirs)

**Files:**
- Create: `pyproject.toml`
- Create: `.python-version`
- Create: `src/dlw/__init__.py`
- Create: `tests/__init__.py`
- Modify: `.gitignore` (append Python entries — they exist but verify)

- [ ] **Step 1: Create `.python-version`**

```
3.12
```

- [ ] **Step 2: Create `pyproject.toml`**

```toml
[project]
name = "dlw"
version = "0.1.0-alpha"
description = "modelpull controller — distributed HuggingFace model downloader"
authors = [{name = "modelpull contributors"}]
license = "Apache-2.0"
readme = "README.md"
requires-python = ">=3.12"

dependencies = [
    "fastapi>=0.115,<0.116",
    "uvicorn[standard]>=0.32,<0.33",
    "sqlalchemy[asyncio]>=2.0,<2.1",
    "asyncpg>=0.29,<0.30",
    "alembic>=1.13,<1.14",
    "pydantic>=2.9,<2.11",
    "pydantic-settings>=2.6,<2.7",
    "structlog>=24.4,<24.5",
    "httpx>=0.27,<0.28",
]

[dependency-groups]
dev = [
    "pytest>=8.3,<9.0",
    "pytest-asyncio>=0.24,<0.25",
    "pytest-cov>=5.0,<6.0",
    "testcontainers[postgres]>=4.8,<5.0",
    "ruff>=0.7,<0.8",
    "mypy>=1.13,<2.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/dlw"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "W", "I", "N", "UP", "B", "C4", "SIM", "RUF"]
ignore = ["E501"]  # line-length handled by formatter

[tool.mypy]
python_version = "3.12"
strict = true
plugins = []

[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
testpaths = ["tests"]
addopts = "-v --tb=short --strict-markers"
markers = [
    "slow: marks tests requiring docker (testcontainers)",
]

[tool.coverage.run]
source = ["src/dlw"]
omit = ["*/alembic/*", "*/__init__.py"]
```

- [ ] **Step 3: Create empty package markers**

`src/dlw/__init__.py`:
```python
"""modelpull controller — distributed HuggingFace model downloader."""

__version__ = "0.1.0-alpha"
```

`tests/__init__.py`:
```python
```

- [ ] **Step 4: Verify uv lockfile generates**

Run:
```bash
uv sync
```

Expected: creates `uv.lock` and `.venv/`; ends with "Resolved N packages".

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock .python-version src/dlw/__init__.py tests/__init__.py
git commit -m "feat(skeleton): initialize Python project with uv

- pyproject.toml: deps for FastAPI + async SQLAlchemy + alembic + pytest
- src layout under src/dlw/
- tests/ for pytest

Refs: docs/v2.0/08-mvp-roadmap.md §1.6 Week 1 Day 1
Plan: docs/superpowers/plans/2026-05-07-phase-1-foundation.md Task 1"
```

---

## Task 2: Docker Compose dev profile (PG 16)

**Files:**
- Create: `docker-compose.dev.yml`

- [ ] **Step 1: Write `docker-compose.dev.yml`**

```yaml
# Dev profile: PG only. Controller / executor run as `uv run uvicorn ...` outside docker.
services:
  postgres:
    image: postgres:16-alpine
    container_name: dlw-postgres-dev
    environment:
      POSTGRES_USER: dlw
      POSTGRES_PASSWORD: dlw_dev_password    # dev only — never in prod
      POSTGRES_DB: dlw
    ports:
      - "5432:5432"
    volumes:
      - dlw-pg-data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U dlw -d dlw"]
      interval: 5s
      timeout: 5s
      retries: 10

volumes:
  dlw-pg-data:
```

- [ ] **Step 2: Bring it up**

```bash
docker compose -f docker-compose.dev.yml up -d
```

Wait 10 seconds, then:

```bash
docker compose -f docker-compose.dev.yml ps
```

Expected: `dlw-postgres-dev` is `Up (healthy)`.

- [ ] **Step 3: Verify connectivity**

```bash
docker compose -f docker-compose.dev.yml exec postgres psql -U dlw -d dlw -c "SELECT version()"
```

Expected: PostgreSQL 16.x server version printed.

- [ ] **Step 4: Commit**

```bash
git add docker-compose.dev.yml
git commit -m "feat(dev): add docker-compose.dev.yml with PG 16

Dev-only PG instance; controller/executor run outside docker for fast iteration.
Password 'dlw_dev_password' is dev-only.

Plan: docs/superpowers/plans/2026-05-07-phase-1-foundation.md Task 2"
```

---

## Task 3: Pytest infrastructure with testcontainer PG

**Files:**
- Create: `tests/conftest.py`
- Create: `tests/db/__init__.py`
- Create: `tests/db/test_smoke.py`

- [ ] **Step 1: Write `tests/conftest.py`**

```python
"""Pytest fixtures: per-test PG instance via testcontainers."""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer


@pytest.fixture(scope="session")
def event_loop():
    """Override default to session-scoped (testcontainers needs it)."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
def pg_container() -> AsyncIterator[PostgresContainer]:
    """One PG container per pytest session (faster than per-test)."""
    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg


@pytest_asyncio.fixture(scope="session")
async def engine(pg_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    """Async SQLAlchemy engine pointing at the testcontainer."""
    # Convert sync URL → async (asyncpg driver)
    url = pg_container.get_connection_url().replace("postgresql+psycopg2://", "postgresql+asyncpg://")
    engine = create_async_engine(url, echo=False, pool_pre_ping=True)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Async session, rolled back after each test for isolation."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
        await session.rollback()
```

- [ ] **Step 2: Write `tests/db/__init__.py` (empty)**

```python
```

- [ ] **Step 3: Write smoke test `tests/db/test_smoke.py`**

```python
"""Smoke test: verify testcontainer PG reachable and async SQLAlchemy works."""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.slow
async def test_pg_reachable(db_session: AsyncSession) -> None:
    result = await db_session.execute(text("SELECT 1 AS one"))
    assert result.scalar() == 1


@pytest.mark.slow
async def test_pg_version(db_session: AsyncSession) -> None:
    result = await db_session.execute(text("SHOW server_version"))
    version = result.scalar()
    assert version is not None
    assert version.startswith("16.")
```

- [ ] **Step 4: Run smoke test**

```bash
uv run pytest tests/db/test_smoke.py -v
```

Expected: Both tests PASS (takes ~10s for first run because Docker pulls image).

- [ ] **Step 5: Commit**

```bash
git add tests/conftest.py tests/db/__init__.py tests/db/test_smoke.py
git commit -m "test: add pytest infrastructure with testcontainer PG

- session-scoped PG container (faster than per-test)
- per-test async session rolled back for isolation
- smoke tests verify connectivity + version

Plan: docs/superpowers/plans/2026-05-07-phase-1-foundation.md Task 3"
```

---

## Task 4: SQLAlchemy base + Config

**Files:**
- Create: `src/dlw/config.py`
- Create: `src/dlw/db/__init__.py`
- Create: `src/dlw/db/base.py`
- Create: `src/dlw/db/session.py`

- [ ] **Step 1: Write `src/dlw/config.py`**

```python
"""Application config via pydantic-settings; env-driven, no hardcoded secrets."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="DLW_",
        extra="ignore",
    )

    db_host: str = Field(default="localhost")
    db_port: int = Field(default=5432)
    db_user: str = Field(default="dlw")
    db_password: str = Field(default="dlw_dev_password")
    db_name: str = Field(default="dlw")

    log_level: str = Field(default="INFO")

    @property
    def db_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 2: Write `src/dlw/db/__init__.py`**

```python
"""DB layer: SQLAlchemy declarative base, session factory, model definitions."""

from dlw.db.base import Base
from dlw.db.session import get_engine, get_session_factory

__all__ = ["Base", "get_engine", "get_session_factory"]
```

- [ ] **Step 3: Write `src/dlw/db/base.py`**

```python
"""Declarative base with explicit naming convention for migrations.

Per docs/v2.0/01-architecture.md §4 — schema must be migration-friendly.
Naming convention follows alembic best-practice (PG-friendly).
"""
from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
```

- [ ] **Step 4: Write `src/dlw/db/session.py`**

```python
"""Async engine + session factory; depend on app config."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from dlw.config import get_settings


def get_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(
        settings.db_url,
        echo=False,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
    )


def get_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
```

- [ ] **Step 5: Verify imports work**

```bash
uv run python -c "from dlw.db import Base, get_engine; print(Base.metadata.naming_convention)"
```

Expected: prints the naming_convention dict (not an ImportError).

- [ ] **Step 6: Commit**

```bash
git add src/dlw/config.py src/dlw/db/__init__.py src/dlw/db/base.py src/dlw/db/session.py
git commit -m "feat(db): SQLAlchemy base + config + async session factory

- pydantic-settings for env-driven config (no hardcoded secrets)
- naming convention ensures migration-friendly schema (PG indexes/FKs)
- async engine with pool_pre_ping + pool_size=10/max_overflow=20

Refs: docs/v2.0/01-architecture.md §4 (data model)
Plan: docs/superpowers/plans/2026-05-07-phase-1-foundation.md Task 4"
```

---

## Task 5: Tenant + Project + User models (TDD)

**Files:**
- Create: `src/dlw/db/models/__init__.py`
- Create: `src/dlw/db/models/tenant.py`
- Create: `tests/db/test_tenant.py`

Models based on `docs/v2.0/01-architecture.md` §4.1.

- [ ] **Step 1: Write failing test `tests/db/test_tenant.py`**

```python
"""Tests for Tenant / Project / User models (architecture §4.1)."""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.base import Base
from dlw.db.models.tenant import Project, Tenant, User


@pytest.fixture(scope="module", autouse=True)
async def _create_tables(engine):
    """Per-module CREATE TABLE so each model file's tests work standalone."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.mark.slow
async def test_tenant_create(db_session: AsyncSession) -> None:
    tenant = Tenant(slug="team-a", display_name="Team A")
    db_session.add(tenant)
    await db_session.commit()
    assert tenant.id is not None
    assert tenant.is_active is True
    assert tenant.quota_concurrent == 10  # default


@pytest.mark.slow
async def test_tenant_slug_unique(db_session: AsyncSession) -> None:
    db_session.add(Tenant(slug="team-b", display_name="B1"))
    await db_session.commit()
    db_session.add(Tenant(slug="team-b", display_name="B2"))
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.slow
async def test_project_belongs_to_tenant(db_session: AsyncSession) -> None:
    tenant = Tenant(slug="team-c", display_name="C")
    db_session.add(tenant)
    await db_session.flush()
    project = Project(tenant_id=tenant.id, name="research")
    db_session.add(project)
    await db_session.commit()
    assert project.tenant_id == tenant.id


@pytest.mark.slow
async def test_user_oidc_subject_unique(db_session: AsyncSession) -> None:
    tenant = Tenant(slug="team-d", display_name="D")
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        User(tenant_id=tenant.id, oidc_subject="keycloak|123", email="a@x.com", role="tenant_admin")
    )
    await db_session.commit()
    db_session.add(
        User(tenant_id=tenant.id, oidc_subject="keycloak|123", email="b@x.com", role="tenant_viewer")
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
```

- [ ] **Step 2: Run test — verify it fails**

```bash
uv run pytest tests/db/test_tenant.py -v
```

Expected: ImportError or ModuleNotFoundError on `dlw.db.models.tenant`.

- [ ] **Step 3: Create `src/dlw/db/models/__init__.py`**

```python
"""ORM models. Importing this module also registers them with Base.metadata."""

from dlw.db.models.tenant import Project, Tenant, User

__all__ = ["Project", "Tenant", "User"]
```

- [ ] **Step 4: Implement `src/dlw/db/models/tenant.py`**

```python
"""Tenant / Project / User models (architecture §4.1)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from dlw.db.base import Base


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    quota_bytes_month: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    quota_concurrent: Mapped[int] = mapped_column(BigInteger, default=10, nullable=False)
    quota_storage_gb: Mapped[int] = mapped_column(BigInteger, default=1024, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    projects: Mapped[list[Project]] = relationship(back_populates="tenant")
    users: Mapped[list[User]] = relationship(back_populates="tenant")


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("tenant_id", "name"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("tenants.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    storage_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    tenant: Mapped[Tenant] = relationship(back_populates="projects")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("tenants.id"), nullable=False)
    oidc_subject: Mapped[str] = mapped_column(String(256), unique=True, nullable=False)
    email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    tenant: Mapped[Tenant] = relationship(back_populates="users")
```

- [ ] **Step 5: Run test — verify it passes**

```bash
uv run pytest tests/db/test_tenant.py -v
```

Expected: 4 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/dlw/db/models/__init__.py src/dlw/db/models/tenant.py tests/db/test_tenant.py
git commit -m "feat(db): Tenant + Project + User models with TDD

- 4 unit tests (create / unique slug / FK / unique oidc_subject)
- naming convention applied via Base
- not yet wired to migrations (Task 11)

Refs: docs/v2.0/01-architecture.md §4.1
Plan: docs/superpowers/plans/2026-05-07-phase-1-foundation.md Task 5"
```

---

## Task 6: StorageBackend model (TDD)

**Files:**
- Create: `src/dlw/db/models/storage.py`
- Create: `tests/db/test_storage.py`

Per `docs/v2.0/01-architecture.md` §4.4. **Note**: `config_encrypted` is `BYTEA` for envelope encryption (Phase 4 wires KMS); for Phase 1 we accept any bytes.

- [ ] **Step 1: Write failing test `tests/db/test_storage.py`**

```python
from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.base import Base
from dlw.db.models.storage import StorageBackend
from dlw.db.models.tenant import Tenant


@pytest.fixture(scope="module", autouse=True)
async def _create_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.mark.slow
async def test_storage_backend_create(db_session: AsyncSession) -> None:
    tenant = Tenant(slug="team-s1", display_name="S1")
    db_session.add(tenant)
    await db_session.flush()
    sb = StorageBackend(
        tenant_id=tenant.id,
        name="prod-s3",
        backend_type="s3",
        region="cn-north-1",
        config_encrypted=b"\x00\x01placeholder",
    )
    db_session.add(sb)
    await db_session.commit()
    assert sb.id is not None
    assert sb.is_default is False


@pytest.mark.slow
async def test_storage_unique_per_tenant(db_session: AsyncSession) -> None:
    tenant = Tenant(slug="team-s2", display_name="S2")
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(StorageBackend(
        tenant_id=tenant.id, name="duplicate", backend_type="s3", config_encrypted=b""
    ))
    await db_session.commit()
    db_session.add(StorageBackend(
        tenant_id=tenant.id, name="duplicate", backend_type="s3", config_encrypted=b""
    ))
    with pytest.raises(IntegrityError):
        await db_session.commit()
```

- [ ] **Step 2: Run test — verify it fails**

```bash
uv run pytest tests/db/test_storage.py -v
```

Expected: ImportError on `dlw.db.models.storage`.

- [ ] **Step 3: Implement `src/dlw/db/models/storage.py`**

```python
"""StorageBackend model (architecture §4.4)."""
from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, ForeignKey, LargeBinary, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from dlw.db.base import Base


class StorageBackend(Base):
    __tablename__ = "storage_backends"
    __table_args__ = (UniqueConstraint("tenant_id", "name"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("tenants.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    backend_type: Mapped[str] = mapped_column(String(32), nullable=False)
    region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # config_encrypted: envelope encryption (KMS); Phase 1 accepts placeholder bytes
    config_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
```

- [ ] **Step 4: Update `src/dlw/db/models/__init__.py`**

```python
"""ORM models. Importing this module also registers them with Base.metadata."""

from dlw.db.models.storage import StorageBackend
from dlw.db.models.tenant import Project, Tenant, User

__all__ = ["Project", "StorageBackend", "Tenant", "User"]
```

- [ ] **Step 5: Run test — verify it passes**

```bash
uv run pytest tests/db/test_storage.py -v
```

Expected: 2 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/dlw/db/models/storage.py src/dlw/db/models/__init__.py tests/db/test_storage.py
git commit -m "feat(db): StorageBackend model with envelope-encrypted config

config_encrypted is LargeBinary; KMS wiring deferred to Phase 4.

Refs: docs/v2.0/01-architecture.md §4.4
Plan: docs/superpowers/plans/2026-05-07-phase-1-foundation.md Task 6"
```

---

## Task 7: DownloadTask + FileSubTask models (TDD)

**Files:**
- Create: `src/dlw/db/models/task.py`
- Create: `tests/db/test_task.py`

Per `docs/v2.0/01-architecture.md` §4.2. **Note**: includes Phase 1 schema **plus** v2.0 fence-token columns from §4.7.1 (left as nullable; logic wires up in Week 2 plan).

- [ ] **Step 1: Write failing test `tests/db/test_task.py`**

```python
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.base import Base
from dlw.db.models.storage import StorageBackend
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.db.models.tenant import Project, Tenant, User


@pytest.fixture(scope="module", autouse=True)
async def _create_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def fixtures(db_session: AsyncSession):
    """Reusable: tenant + project + user + storage_backend."""
    tenant = Tenant(slug=f"t-{uuid.uuid4().hex[:6]}", display_name="T")
    db_session.add(tenant)
    await db_session.flush()
    project = Project(tenant_id=tenant.id, name="research")
    db_session.add(project)
    user = User(
        tenant_id=tenant.id,
        oidc_subject=f"oidc-{uuid.uuid4()}",
        email="x@y.com",
        role="tenant_admin",
    )
    db_session.add(user)
    sb = StorageBackend(
        tenant_id=tenant.id, name="s3-prod", backend_type="s3", config_encrypted=b""
    )
    db_session.add(sb)
    await db_session.flush()
    return tenant, project, user, sb


@pytest.mark.slow
async def test_create_task(db_session: AsyncSession, fixtures) -> None:
    tenant, project, user, sb = fixtures
    task = DownloadTask(
        tenant_id=tenant.id,
        project_id=project.id,
        owner_user_id=user.id,
        repo_id="deepseek-ai/DeepSeek-V3",
        revision="abc123def4567890abc123def4567890abc12345",
        storage_id=sb.id,
        path_template="{tenant}/{repo_id}/{revision}",
        priority=2,
        status="pending",
    )
    db_session.add(task)
    await db_session.commit()
    assert task.id is not None
    assert task.is_simulation is False
    assert task.created_at is not None


@pytest.mark.slow
async def test_subtask_belongs_to_task(db_session: AsyncSession, fixtures) -> None:
    tenant, project, user, sb = fixtures
    task = DownloadTask(
        tenant_id=tenant.id,
        project_id=project.id,
        owner_user_id=user.id,
        repo_id="org/repo",
        revision="0" * 40,
        storage_id=sb.id,
        path_template="{tenant}/{repo_id}",
        status="pending",
    )
    db_session.add(task)
    await db_session.flush()
    subtask = FileSubTask(
        task_id=task.id,
        tenant_id=tenant.id,
        filename="model.safetensors",
        file_size=1_000_000,
        expected_sha256="abcd" * 16,
        status="pending",
    )
    db_session.add(subtask)
    await db_session.commit()
    assert subtask.id is not None
    assert subtask.chunks_completed == 0


@pytest.mark.slow
async def test_subtask_unique_filename_per_task(db_session: AsyncSession, fixtures) -> None:
    tenant, project, user, sb = fixtures
    task = DownloadTask(
        tenant_id=tenant.id, project_id=project.id, owner_user_id=user.id,
        repo_id="o/r", revision="0" * 40, storage_id=sb.id,
        path_template="{tenant}", status="pending",
    )
    db_session.add(task)
    await db_session.flush()
    db_session.add(FileSubTask(
        task_id=task.id, tenant_id=tenant.id, filename="dup.bin", status="pending",
    ))
    await db_session.commit()
    db_session.add(FileSubTask(
        task_id=task.id, tenant_id=tenant.id, filename="dup.bin", status="pending",
    ))
    with pytest.raises(IntegrityError):
        await db_session.commit()
```

- [ ] **Step 2: Run test — verify it fails**

```bash
uv run pytest tests/db/test_task.py -v
```

Expected: ImportError on `dlw.db.models.task`.

- [ ] **Step 3: Implement `src/dlw/db/models/task.py`**

```python
"""DownloadTask + FileSubTask models (architecture §4.2 + §4.7.1).

v2.0 schema includes Phase 2/v2.1 fence-token columns as nullable here;
logic that uses them comes in Week 2's plan.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from dlw.db.base import Base


class DownloadTask(Base):
    __tablename__ = "download_tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("tenants.id"), nullable=False)
    project_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("projects.id"), nullable=False)
    owner_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    repo_id: Mapped[str] = mapped_column(String(256), nullable=False)
    revision: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("storage_backends.id"), nullable=False
    )
    path_template: Mapped[str] = mapped_column(String(512), nullable=False)
    priority: Mapped[int] = mapped_column(SmallInteger, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    is_simulation: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    download_bytes_limit: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    upgrade_from_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class FileSubTask(Base):
    __tablename__ = "file_subtasks"
    __table_args__ = (UniqueConstraint("task_id", "filename"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("download_tasks.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)  # denormalized for query
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    expected_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actual_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)

    executor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Fence-token columns (Phase 2 wires logic; Phase 1 leaves nullable)
    executor_epoch: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    assignment_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    chunks_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunks_completed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    bytes_downloaded: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    multipart_upload_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

- [ ] **Step 4: Update `src/dlw/db/models/__init__.py`**

```python
from dlw.db.models.storage import StorageBackend
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.db.models.tenant import Project, Tenant, User

__all__ = [
    "DownloadTask", "FileSubTask",
    "Project", "StorageBackend", "Tenant", "User",
]
```

- [ ] **Step 5: Run test — verify it passes**

```bash
uv run pytest tests/db/test_task.py -v
```

Expected: 3 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/dlw/db/models/task.py src/dlw/db/models/__init__.py tests/db/test_task.py
git commit -m "feat(db): DownloadTask + FileSubTask models with fence-token columns

executor_epoch + assignment_token nullable for Phase 1; logic in Week 2 plan.
UNIQUE(task_id, filename) enforces sub-task identity per file.

Refs: docs/v2.0/01-architecture.md §4.2 + §4.7.1
Plan: docs/superpowers/plans/2026-05-07-phase-1-foundation.md Task 7"
```

---

## Task 8: Executor model (TDD)

**Files:**
- Create: `src/dlw/db/models/executor.py`
- Create: `tests/db/test_executor.py`

Per `docs/v2.0/01-architecture.md` §4.3.

- [ ] **Step 1: Write failing test `tests/db/test_executor.py`**

```python
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.base import Base
from dlw.db.models.executor import Executor


@pytest.fixture(scope="module", autouse=True)
async def _create_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.mark.slow
async def test_executor_create(db_session: AsyncSession) -> None:
    executor = Executor(
        id="host-12.local-worker-1",
        host_id="host-12.local",
        cert_fingerprint="placeholder-phase1",
        status="joining",
    )
    db_session.add(executor)
    await db_session.commit()
    assert executor.health_score == 100  # default
    assert executor.consecutive_heartbeat_failures == 0


@pytest.mark.slow
async def test_executor_capabilities_jsonb(db_session: AsyncSession) -> None:
    executor = Executor(
        id="host-13.local-worker-1",
        host_id="host-13.local",
        cert_fingerprint="placeholder-phase1",
        status="healthy",
        capabilities={"nic_speed_gbps": 10, "regions": ["cn-north-1"]},
    )
    db_session.add(executor)
    await db_session.commit()
    await db_session.refresh(executor)
    assert executor.capabilities["nic_speed_gbps"] == 10
    assert "cn-north-1" in executor.capabilities["regions"]
```

- [ ] **Step 2: Run test — verify it fails**

```bash
uv run pytest tests/db/test_executor.py -v
```

Expected: ImportError on `dlw.db.models.executor`.

- [ ] **Step 3: Implement `src/dlw/db/models/executor.py`**

```python
"""Executor model (architecture §4.3)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, SmallInteger, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from dlw.db.base import Base


class Executor(Base):
    __tablename__ = "executors"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("tenants.id"), nullable=True
    )
    host_id: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_executor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cert_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    health_score: Mapped[int] = mapped_column(SmallInteger, default=100, nullable=False)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    consecutive_heartbeat_failures: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    consecutive_task_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    degraded_failure_streak: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    nic_speed_gbps: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    disk_free_gb: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    disk_total_gb: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    parts_dir_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

- [ ] **Step 4: Update `__init__.py`**

```python
from dlw.db.models.executor import Executor
from dlw.db.models.storage import StorageBackend
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.db.models.tenant import Project, Tenant, User

__all__ = [
    "DownloadTask", "Executor", "FileSubTask",
    "Project", "StorageBackend", "Tenant", "User",
]
```

- [ ] **Step 5: Run test — verify it passes**

```bash
uv run pytest tests/db/test_executor.py -v
```

Expected: 2 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/dlw/db/models/executor.py src/dlw/db/models/__init__.py tests/db/test_executor.py
git commit -m "feat(db): Executor model with JSONB capabilities

executor.id is varchar (host-12.local-worker-1 style); see invariant 9 +
01 §4.3. v2.1 columns (epoch / display_name / etc.) added in Phase 2 plan.

Refs: docs/v2.0/01-architecture.md §4.3
Plan: docs/superpowers/plans/2026-05-07-phase-1-foundation.md Task 8"
```

---

## Task 9: AuditLog model (TDD; chain-hash deferred)

**Files:**
- Create: `src/dlw/db/models/audit.py`
- Create: `tests/db/test_audit.py`

Per `docs/v2.0/04-security-and-tenancy.md` §9.1. **Note**: chain-hash trigger logic comes in Phase 4 plan; Phase 1 just stores the columns.

- [ ] **Step 1: Write failing test `tests/db/test_audit.py`**

```python
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.base import Base
from dlw.db.models.audit import AuditLog


@pytest.fixture(scope="module", autouse=True)
async def _create_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.mark.slow
async def test_audit_log_create(db_session: AsyncSession) -> None:
    entry = AuditLog(
        action="task.create",
        resource_type="download_tasks",
        resource_id="some-uuid",
        outcome="success",
        payload={"repo_id": "deepseek-ai/DeepSeek-V3"},
        prev_hash="0" * 64,
        self_hash="a" * 64,
    )
    db_session.add(entry)
    await db_session.commit()
    assert entry.id is not None
    assert entry.occurred_at is not None
```

- [ ] **Step 2: Run test — verify it fails**

```bash
uv run pytest tests/db/test_audit.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement `src/dlw/db/models/audit.py`**

```python
"""AuditLog model (security §9.1).

Phase 1: schema only. Chain-hash trigger / verifier in Phase 4 plan.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, String, func
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from dlw.db.base import Base


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    tenant_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    actor_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    actor_ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    prev_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    self_hash: Mapped[str] = mapped_column(String(64), nullable=False)
```

- [ ] **Step 4: Update `__init__.py`**

```python
from dlw.db.models.audit import AuditLog
from dlw.db.models.executor import Executor
from dlw.db.models.storage import StorageBackend
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.db.models.tenant import Project, Tenant, User

__all__ = [
    "AuditLog", "DownloadTask", "Executor", "FileSubTask",
    "Project", "StorageBackend", "Tenant", "User",
]
```

- [ ] **Step 5: Run test — verify it passes**

```bash
uv run pytest tests/db/test_audit.py -v
```

Expected: 1 test PASS.

- [ ] **Step 6: Commit**

```bash
git add src/dlw/db/models/audit.py src/dlw/db/models/__init__.py tests/db/test_audit.py
git commit -m "feat(db): AuditLog model (chain-hash deferred to Phase 4)

Schema landed; trigger / hash-chain verifier in Phase 4 plan.

Refs: docs/v2.0/04-security-and-tenancy.md §9.1
Plan: docs/superpowers/plans/2026-05-07-phase-1-foundation.md Task 9"
```

---

## Task 10: Alembic init + async env.py

**Files:**
- Create: `alembic.ini`
- Create: `src/dlw/alembic/env.py`
- Create: `src/dlw/alembic/script.py.mako`
- Create: `src/dlw/alembic/versions/.gitkeep`

- [ ] **Step 1: Initialize alembic**

```bash
uv run alembic init -t async src/dlw/alembic
```

Expected: creates `alembic.ini` (root) + `src/dlw/alembic/{env.py, script.py.mako, versions/}`.

- [ ] **Step 2: Edit `alembic.ini` — point at `src/dlw/alembic/`**

Update `script_location` line:

```ini
script_location = src/dlw/alembic
```

Also remove or comment out the auto-set `sqlalchemy.url`; we'll set it from `config.py` in `env.py`:

```ini
# sqlalchemy.url = (commented; set in env.py from dlw.config)
```

- [ ] **Step 3: Replace `src/dlw/alembic/env.py`**

```python
"""Async-aware alembic env using dlw.config + Base.metadata."""
from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Import all models so Base.metadata is fully populated
from dlw.config import get_settings
from dlw.db import Base
from dlw.db.models import (  # noqa: F401  side-effect import
    AuditLog,
    DownloadTask,
    Executor,
    FileSubTask,
    Project,
    StorageBackend,
    Tenant,
    User,
)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    """Override sqlalchemy.url from app settings."""
    return get_settings().db_url


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    config_section = config.get_section(config.config_ini_section, {})
    config_section["sqlalchemy.url"] = get_url()
    connectable = async_engine_from_config(
        config_section, prefix="sqlalchemy.", future=True
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

- [ ] **Step 4: Verify alembic can connect**

With docker compose PG running:

```bash
uv run alembic current
```

Expected: prints nothing (no migrations applied yet) and exits 0.

- [ ] **Step 5: Commit**

```bash
git add alembic.ini src/dlw/alembic/
git commit -m "feat(alembic): async-aware env.py wired to dlw.config

env.py imports all models via dlw.db.models so Base.metadata is complete.
sqlalchemy.url is sourced from get_settings() at runtime, not hardcoded.

Plan: docs/superpowers/plans/2026-05-07-phase-1-foundation.md Task 10"
```

---

## Task 11: First migration (autogenerate + commit)

**Files:**
- Create: `src/dlw/alembic/versions/<auto>.py`

- [ ] **Step 1: Generate initial migration via autogenerate**

Ensure docker-compose PG is running, then:

```bash
uv run alembic revision --autogenerate -m "initial v2 schema (tenants/users/projects/storage/tasks/subtasks/executors/audit_log)"
```

Expected: creates `src/dlw/alembic/versions/<some_hash>_initial_v2_schema_*.py`.

- [ ] **Step 2: Review the generated migration**

Open the generated file. Verify:
- 8 `op.create_table()` calls (tenants, projects, users, storage_backends, download_tasks, file_subtasks, executors, audit_log)
- Foreign keys present (tenant_id, project_id, etc.)
- Unique constraints present (`uq_users_oidc_subject`, `uq_storage_backends_*`, `uq_file_subtasks_*`)

If autogenerate produced a wrong order (FK before parent table), reorder manually.

Add explicit `default=` for server_default columns missed by autogenerate (rare).

- [ ] **Step 3: Apply the migration to dev PG**

```bash
uv run alembic upgrade head
```

Expected: runs without error; ends with "INFO ... Running upgrade ... -> <revision_hash>".

- [ ] **Step 4: Verify schema landed in PG**

```bash
docker compose -f docker-compose.dev.yml exec postgres psql -U dlw -d dlw -c "\dt"
```

Expected output:
```
                List of relations
 Schema |       Name         | Type  | Owner
--------+--------------------+-------+-------
 public | alembic_version    | table | dlw
 public | audit_log          | table | dlw
 public | download_tasks     | table | dlw
 public | executors          | table | dlw
 public | file_subtasks      | table | dlw
 public | projects           | table | dlw
 public | storage_backends   | table | dlw
 public | tenants            | table | dlw
 public | users              | table | dlw
(9 rows)
```

- [ ] **Step 5: Test rollback**

```bash
uv run alembic downgrade -1
docker compose -f docker-compose.dev.yml exec postgres psql -U dlw -d dlw -c "\dt"
```

Expected: only `alembic_version` table remains.

Then re-upgrade:
```bash
uv run alembic upgrade head
```

- [ ] **Step 6: Commit**

```bash
git add src/dlw/alembic/versions/
git commit -m "feat(alembic): initial migration — 8 tables for v2.0 Phase 1 schema

Autogenerated then manually verified. Round-trip tested:
  upgrade head → downgrade -1 → upgrade head

Refs: docs/v2.0/09-migration.md §2.1 (matches Phase 1 schema list)
Plan: docs/superpowers/plans/2026-05-07-phase-1-foundation.md Task 11"
```

---

## Task 12: Migration round-trip integration test

**Files:**
- Create: `tests/db/test_alembic.py`

- [ ] **Step 1: Write `tests/db/test_alembic.py`**

```python
"""Integration test: alembic migration is round-trip-safe."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_TABLES = {
    "alembic_version",
    "audit_log",
    "download_tasks",
    "executors",
    "file_subtasks",
    "projects",
    "storage_backends",
    "tenants",
    "users",
}


def _alembic(args: list[str], db_url: str) -> None:
    """Run alembic CLI with DLW_DB_* env from URL.

    URL format: postgresql+asyncpg://user:pwd@host:port/db
    """
    from urllib.parse import urlparse

    parsed = urlparse(db_url.replace("postgresql+asyncpg://", "postgresql://"))
    env = {
        "DLW_DB_HOST": parsed.hostname or "localhost",
        "DLW_DB_PORT": str(parsed.port or 5432),
        "DLW_DB_USER": parsed.username or "dlw",
        "DLW_DB_PASSWORD": parsed.password or "",
        "DLW_DB_NAME": (parsed.path or "/dlw").lstrip("/"),
        "PATH": __import__("os").environ.get("PATH", ""),
    }
    result = subprocess.run(
        ["uv", "run", "alembic", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"alembic {args} failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"


@pytest.mark.slow
async def test_upgrade_head_creates_all_tables(engine: AsyncEngine, pg_container) -> None:
    """alembic upgrade head produces expected table set."""
    db_url = pg_container.get_connection_url().replace(
        "postgresql+psycopg2://", "postgresql+asyncpg://"
    )
    _alembic(["upgrade", "head"], db_url)
    async with engine.connect() as conn:

        def _get_tables(sync_conn):
            return set(inspect(sync_conn).get_table_names())

        actual = await conn.run_sync(_get_tables)
    assert actual == EXPECTED_TABLES, f"Tables mismatch: {actual ^ EXPECTED_TABLES}"


@pytest.mark.slow
async def test_downgrade_rolls_back(engine: AsyncEngine, pg_container) -> None:
    """alembic downgrade -1 leaves only alembic_version."""
    db_url = pg_container.get_connection_url().replace(
        "postgresql+psycopg2://", "postgresql+asyncpg://"
    )
    _alembic(["upgrade", "head"], db_url)
    _alembic(["downgrade", "-1"], db_url)
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'"))
        actual = {row[0] for row in result.all()}
    assert actual == {"alembic_version"}, f"Expected only alembic_version after downgrade, got {actual}"
    # Restore for subsequent tests
    _alembic(["upgrade", "head"], db_url)
```

- [ ] **Step 2: Run the test**

```bash
uv run pytest tests/db/test_alembic.py -v
```

Expected: 2 tests PASS (slower — runs alembic CLI).

- [ ] **Step 3: Commit**

```bash
git add tests/db/test_alembic.py
git commit -m "test(alembic): migration round-trip — upgrade head + downgrade -1

Verifies 9 tables present after upgrade; only alembic_version after downgrade.
Run via subprocess to mirror real CI / oncall workflow.

Plan: docs/superpowers/plans/2026-05-07-phase-1-foundation.md Task 12"
```

---

## Task 13: FastAPI app + health endpoints

**Files:**
- Create: `src/dlw/main.py`
- Create: `src/dlw/api/__init__.py`
- Create: `src/dlw/api/health.py`
- Create: `tests/api/__init__.py`
- Create: `tests/api/test_health.py`

- [ ] **Step 1: Write failing test `tests/api/test_health.py`**

```python
"""Health endpoint smoke tests."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from dlw.main import create_app


@pytest.mark.slow
async def test_health_live_returns_200() -> None:
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


@pytest.mark.slow
async def test_health_ready_returns_200_when_db_reachable(pg_container) -> None:
    """Ready check requires DB; we reuse pg_container as backing PG."""
    import os
    from urllib.parse import urlparse

    parsed = urlparse(pg_container.get_connection_url().replace("postgresql+psycopg2://", "postgresql://"))
    os.environ["DLW_DB_HOST"] = parsed.hostname or "localhost"
    os.environ["DLW_DB_PORT"] = str(parsed.port or 5432)
    os.environ["DLW_DB_USER"] = parsed.username or "dlw"
    os.environ["DLW_DB_PASSWORD"] = parsed.password or ""
    os.environ["DLW_DB_NAME"] = (parsed.path or "/dlw").lstrip("/")

    # Re-create settings cache
    from dlw.config import get_settings
    get_settings.cache_clear()

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["db"] == "ok"
```

- [ ] **Step 2: Run test — verify it fails**

```bash
uv run pytest tests/api/test_health.py -v
```

Expected: ImportError on `dlw.main`.

- [ ] **Step 3: Implement `src/dlw/api/__init__.py`**

```python
```

- [ ] **Step 4: Implement `src/dlw/api/health.py`**

```python
"""Health endpoints: /health/live (process up?) + /health/ready (db reachable?)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.db.session import get_engine

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def live() -> dict[str, str]:
    """Liveness — always 200 if process responding (used by k8s livenessProbe)."""
    return {"status": "healthy"}


@router.get("/ready")
async def ready() -> dict[str, str]:
    """Readiness — DB-dependent (used by k8s readinessProbe + LB)."""
    engine = get_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ready", "db": "ok"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"db unreachable: {exc}") from exc
    finally:
        await engine.dispose()
```

- [ ] **Step 5: Implement `src/dlw/main.py`**

```python
"""FastAPI app factory + lifespan."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from dlw.api.health import router as health_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Lifespan hook — wire up shared resources here in later phases."""
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="modelpull controller",
        version="0.1.0-alpha",
        lifespan=lifespan,
    )
    app.include_router(health_router)
    return app


# uvicorn target: dlw.main:app
app = create_app()
```

- [ ] **Step 6: Run test — verify it passes**

```bash
uv run pytest tests/api/test_health.py -v
```

Expected: 2 tests PASS.

- [ ] **Step 7: End-to-end smoke — start server manually, hit endpoints**

In one terminal:

```bash
uv run uvicorn dlw.main:app --host 0.0.0.0 --port 8000
```

In another terminal:

```bash
curl -s http://localhost:8000/health/live | head -c 200
```

Expected: `{"status":"healthy"}`

```bash
curl -s http://localhost:8000/health/ready | head -c 200
```

Expected: `{"status":"ready","db":"ok"}` (assuming docker-compose PG running).

Stop the uvicorn process (Ctrl-C).

- [ ] **Step 8: Commit**

```bash
git add src/dlw/main.py src/dlw/api/ tests/api/
git commit -m "feat(api): FastAPI app + health endpoints (live + ready)

/health/live — always 200 (k8s livenessProbe target)
/health/ready — verifies DB reachable (k8s readinessProbe + LB)
2 unit tests via httpx ASGI transport.

Smoke: \`uv run uvicorn dlw.main:app\` + \`curl localhost:8000/health/live\`

Plan: docs/superpowers/plans/2026-05-07-phase-1-foundation.md Task 13"
```

---

## Task 14: CI integration — run pytest

**Files:**
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: View current CI**

```bash
grep -n "name:" .github/workflows/ci.yml | head -20
```

Expected: existing jobs visible (markdown lint, helm lint, etc.). We're adding a new `pytest` job that doesn't conflict.

- [ ] **Step 2: Append pytest job to `.github/workflows/ci.yml`**

Find the line `  invariant_lint:` and add a new job ABOVE the `ci:` aggregation job:

```yaml
  # ============================================================
  # Python: pytest with testcontainers PG (Phase 1 foundation)
  # ============================================================
  pytest:
    name: pytest (Phase 1 foundation)
    runs-on: ubuntu-latest
    services:
      docker:
        image: docker:dind
    steps:
      - uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with: {python-version: '3.12'}

      - name: Install uv
        uses: astral-sh/setup-uv@v3
        with: {version: '0.5.0'}

      - name: Install deps via uv
        run: uv sync --all-groups

      - name: Run pytest
        run: |
          uv run pytest tests/ \
            -v \
            --tb=short \
            --cov=src/dlw \
            --cov-report=term-missing \
            --cov-report=xml

      - name: Upload coverage
        uses: actions/upload-artifact@v4
        with:
          name: coverage-report
          path: coverage.xml
```

Then update the aggregation job's `needs:` list to include `pytest`:

```yaml
  ci:
    name: CI Status
    needs: [openapi, helm, shellcheck, markdown, yamllint, security, json, invariant_lint, pytest]
```

- [ ] **Step 3: Commit and let CI run**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: run pytest on every PR (Phase 1 foundation)

Adds pytest job that uses testcontainers for PG; coverage uploaded as artifact.
Aggregation gate updated to include pytest.

Plan: docs/superpowers/plans/2026-05-07-phase-1-foundation.md Task 14"

git push origin <your-branch>
```

- [ ] **Step 4: Verify CI passes**

```bash
gh run list --limit 1
```

Wait for the run to complete:

```bash
RUN_ID=$(gh run list --limit 1 --json databaseId --jq '.[0].databaseId') && \
  until gh run view "$RUN_ID" --json status --jq '.status' | grep -q completed; do sleep 15; done && \
  gh run view "$RUN_ID" --json conclusion --jq '.conclusion'
```

Expected: `success`. If pytest job fails, check the log:
```bash
gh run view "$RUN_ID" --log-failed | grep -E "FAILED|Error" | head -20
```

---

## Task 15: README quickstart

**Files:**
- Modify: `README.md` (add quickstart section)

- [ ] **Step 1: Find current README structure**

```bash
grep -n "^## " README.md | head -10
```

We'll insert a "Quickstart (developer)" section after the existing positioning header.

- [ ] **Step 2: Add Quickstart section**

Open `README.md` and after the "⚡ 现在你可以做的 3 件事" section, before "为什么做这个", insert:

```markdown
## 🚀 Quickstart (Phase 1 dev only)

> Phase 1 已落地：FastAPI controller + PG schema + 健康检查。
> 不能下载模型；这是后续 Week 2-6 实施的基础。

```bash
# 1. 拉代码
git clone https://github.com/l17728/modelpull && cd modelpull

# 2. 装 uv（如未装）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 3. 装 deps
uv sync

# 4. 起 PG
docker compose -f docker-compose.dev.yml up -d

# 5. 跑 migration
uv run alembic upgrade head

# 6. 起 controller
uv run uvicorn dlw.main:app --host 0.0.0.0 --port 8000

# 7. 测试 endpoints（另开终端）
curl http://localhost:8000/health/live    # → {"status":"healthy"}
curl http://localhost:8000/health/ready   # → {"status":"ready","db":"ok"}

# 8. 跑 tests
uv run pytest -v
```

完整开发计划：[`docs/superpowers/plans/2026-05-07-phase-1-foundation.md`](./docs/superpowers/plans/2026-05-07-phase-1-foundation.md)
```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(readme): add Phase 1 dev quickstart (8 commands to running controller)

Covers: clone → uv sync → docker compose → alembic upgrade → uvicorn → curl.
Anchors quickstart to this plan file for traceability.

Plan: docs/superpowers/plans/2026-05-07-phase-1-foundation.md Task 15"
```

---

## Acceptance criteria — done when ALL of these hold

- [ ] `docker compose -f docker-compose.dev.yml ps` shows PG `Up (healthy)`
- [ ] `uv run alembic current` shows current revision matches initial migration hash
- [ ] `psql ... -c "\dt"` shows 9 tables (8 + `alembic_version`)
- [ ] `uv run pytest -v` shows all green; >= 14 tests passing (1 smoke + 4 tenant + 2 storage + 3 task + 2 executor + 1 audit + 2 alembic)
- [ ] `uv run uvicorn dlw.main:app` starts without error
- [ ] `curl localhost:8000/health/live` returns `{"status":"healthy"}`
- [ ] `curl localhost:8000/health/ready` returns `{"status":"ready","db":"ok"}`
- [ ] CI `pytest` job green on a PR branch
- [ ] All 15 tasks committed; `git log --oneline | head -20` shows clean history

---

## What's NOT done — Week 2's plan picks up

This plan deliberately stops here. Week 2's plan ([`2026-05-XX-phase-1-week-2-controller-core.md`](.) — yet to be written) will:

- Add task CRUD API (`POST/GET/PATCH /api/tasks`)
- Add scheduler loop + CAS-then-enqueue (invariant 6, simplified — no fence yet)
- Wire up basic auth (Bearer token; OIDC PKCE in Phase 3)
- Heartbeat endpoint (no mTLS yet; Phase 2)
- WebSocket progress fan-out
- Per-tenant filter on queries (still hard-coded `tenant_id=1`)

Then Week 3 plan: Executor process + Vue 3 UI scaffold.
Then Week 4 plan: streaming SHA256 + E2E-001.
Then Week 5 plan: dev infra (FEAS-06).
Then Week 6 plan: buffer + alpha demo.

---

## Plan self-review (DRY / placeholders / type consistency)

**Spec coverage:**
- ✅ docs/v2.0/01-architecture.md §4 — all Phase 1 tables landed
- ✅ docs/v2.0/08-mvp-roadmap.md §1.6 Week 1 — covered Days 1-2 in detail; Days 3-5 require Week 2 plan
- ✅ docs/v2.0/09-migration.md §2.1 — schema matches autogenerated migration
- ✅ docs/v2.0/05-operations.md §1 — health endpoints align with k8s probe spec

**Placeholder scan:**
- No "TODO" / "TBD" / "implement later" used
- All code blocks contain real, runnable code
- Cross-task refs are explicit (Task N references model/file/import already defined)

**Type consistency:**
- `Tenant.id` is `BigInteger` everywhere
- `DownloadTask.id` / `FileSubTask.id` / `Project.id` etc. all use UUID where doc spec says UUID; BigInteger where spec says BIGSERIAL
- `tenant_id` is `BigInteger` (matches FK target)
- `status` columns are `String(32)` — consistent
- `.gitkeep` placeholder for `versions/` removed once first migration auto-generated

**Frequent commits:**
- 15 tasks → 15 commits (one per task; each task = TDD red→green→commit cycle)

**TDD adherence:**
- Tasks 5-9 (models): test → fail → impl → pass → commit
- Tasks 12-13 (alembic test, health endpoint): test → fail → impl → pass → commit
- Task 11 (alembic migration): autogenerate then verify (autogen is the impl; running `upgrade head` is the verify)
- Tasks 1-4, 10, 14, 15 are scaffolding/infra (no TDD per se) — code is verified by `uv sync` / `alembic current` / CI green

**YAGNI:**
- v2.1 columns (display_name, location, tags, credential_pool, ws_session) NOT in this plan — they go in their respective Phase plans
- AI Copilot / Optimizer / Adaptive scheduling tables NOT in this plan
- Audit chain-hash trigger NOT in this plan (Phase 4)
- mTLS / fence token logic NOT in this plan (Phase 2)
