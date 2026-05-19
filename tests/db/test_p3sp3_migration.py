"""SP3 migration: storage_objects + subtask_object_refs + inherit_from_key."""
from __future__ import annotations

import pytest
from sqlalchemy import text

import dlw.db.models  # noqa: F401

pytestmark = pytest.mark.slow


async def test_tables_and_column(engine):
    from dlw.db.base import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        names = {r[0] for r in await conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public'"))}
        assert {"storage_objects", "subtask_object_refs"} <= names
        scols = {r[0] for r in await conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='file_subtasks'"))}
        assert "inherit_from_key" in scols
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def test_unique_tenant_storage_sha(engine):
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from dlw.db.base import Base
    from dlw.db.models.storage_object import StorageObject
    from dlw.db.models.tenant import Tenant
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add(Tenant(id=1, slug="t", display_name="T"))
        await s.commit()
        s.add(StorageObject(tenant_id=1, storage_id=1, storage_key="k1",
                            sha256="a" * 64, size=10))
        await s.commit()
        s.add(StorageObject(tenant_id=1, storage_id=1, storage_key="k2",
                            sha256="a" * 64, size=10))
        with pytest.raises(IntegrityError):
            await s.commit()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
