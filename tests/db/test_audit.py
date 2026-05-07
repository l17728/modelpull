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
