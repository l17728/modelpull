"""SP2 migration: 3 tables + task/subtask source columns."""
from __future__ import annotations

import pytest
from sqlalchemy import text

import dlw.db.models  # noqa: F401

pytestmark = pytest.mark.slow


async def test_tables_and_columns(engine):
    from dlw.db.base import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        names = {r[0] for r in await conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public'"))}
        assert {"subtask_chunks", "source_speed_samples",
                "source_blacklist"} <= names
        cols = {r[0] for r in await conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='download_tasks'"))}
        assert {"source_strategy", "source_blacklist",
                "trust_non_hf_sha256"} <= cols
        scols = {r[0] for r in await conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='file_subtasks'"))}
        assert {"source_id", "is_chunked"} <= scols
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
