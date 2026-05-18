"""SP1 migration: 3 new tables + idempotent default-tenant/snapshot seed."""
from __future__ import annotations

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.slow


async def _tables(conn) -> set[str]:
    rows = await conn.execute(text(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public'"))
    return {r[0] for r in rows}


async def test_upgrade_creates_tables_and_seeds(engine):
    from dlw.db.base import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    from dlw.alembic.versions import _p3sp1_seed  # helper module (see impl)
    async with engine.begin() as conn:
        names = await _tables(conn)
        assert {"usage_records", "quota_snapshots", "casbin_rule"} <= names
    from sqlalchemy.ext.asyncio import async_sessionmaker
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        await _p3sp1_seed.seed(s)
        await _p3sp1_seed.seed(s)  # idempotent: second call no-op
        await s.commit()
        from sqlalchemy import select

        from dlw.db.models.tenant import Tenant
        from dlw.db.models.usage import QuotaSnapshot
        t = (await s.execute(select(Tenant).where(Tenant.id == 1))).scalar_one()
        assert t.slug == "default"
        snap = (await s.execute(
            select(QuotaSnapshot).where(QuotaSnapshot.tenant_id == 1))
        ).scalar_one()
        assert snap.bytes_used_month == 0
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
