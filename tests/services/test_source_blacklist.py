"""Source blacklist transitions (Phase 3 SP2; doc §1.7)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

import dlw.db.models  # noqa: F401
from dlw.db.base import Base
from dlw.db.models.source import SourceBlacklist
from dlw.services.source_blacklist import (
    blacklist_file,
    is_blacklisted,
)

pytestmark = pytest.mark.slow


@pytest.fixture
async def factory(engine):
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)
        await c.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)


async def test_blacklist_and_check(factory):
    async with factory() as s:
        await blacklist_file(s, source_id="modelscope", repo_id="o/r",
                             filename="m.safetensors", hours=24,
                             reason="sha_mismatch")
        await s.commit()
        assert await is_blacklisted(s, "modelscope", "o/r",
                                    "m.safetensors") is True
        assert await is_blacklisted(s, "modelscope", "o/r",
                                    "other.bin") is False


async def test_expired_not_blacklisted(factory):
    async with factory() as s:
        s.add(SourceBlacklist(source_id="modelscope", repo_id="o/r",
                              filename="m", reason="x",
                              until=datetime.now(UTC) - timedelta(hours=1)))
        await s.commit()
        assert await is_blacklisted(s, "modelscope", "o/r", "m") is False
