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
ALPHA_DEMO_REVISION = "c9745ed1d9f207416be6d2e6f8de32d1f16199bf"   # pinned per docs/demo/dataset-catalog.md


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
