"""Tests for Tenant / Project / User models (architecture §4.1)."""
from __future__ import annotations

import pytest
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
