"""Smoke test: verify local PG reachable and async SQLAlchemy works."""
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
    # Accept PG 16, 17, 18 (local dev uses 18; CI pins 16)
    assert version.split(".")[0] in {"16", "17", "18"}, f"Unexpected PG version: {version}"
