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
