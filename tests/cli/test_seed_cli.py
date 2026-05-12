"""Tests for dlw-seed CLI — subprocess against the per-session test DB."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.base import Base
from dlw.db.models.storage import StorageBackend
from dlw.db.models.task import DownloadTask


def _env_for_subprocess(test_db_name: str) -> dict[str, str]:
    """Build env var dict pointing CLI at the per-session test DB."""
    return {
        **{k: v for k, v in os.environ.items() if not k.startswith("DLW_DB_")},
        "DLW_DB_HOST": os.environ.get("DLW_TEST_PG_HOST", "localhost"),
        "DLW_DB_PORT": os.environ.get("DLW_TEST_PG_PORT", "5433"),
        "DLW_DB_USER": os.environ.get("DLW_TEST_PG_USER", "postgres"),
        "DLW_DB_PASSWORD": os.environ.get("DLW_TEST_PG_PASSWORD", ""),
        "DLW_DB_NAME": test_db_name,
        "DLW_BEARER_TOKEN": "ignored-by-seed",   # CLI doesn't use it but Settings validates
    }


@pytest.fixture(scope="module", autouse=True)
async def _create_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.mark.slow
def test_seed_cli_no_arg_errors() -> None:
    """Without --default or --demo, argparse must exit 2 with a usage message."""
    r = subprocess.run(
        [sys.executable, "-m", "dlw.cli.seed"],
        capture_output=True, text=True, timeout=15,
    )
    assert r.returncode == 2
    combined = r.stdout + r.stderr
    assert "default" in combined or "demo" in combined


@pytest.mark.slow
async def test_seed_cli_default_runs(
    test_db_name: str, db_session: AsyncSession,
) -> None:
    """`dlw-seed --default` against test DB inserts 4 rows."""
    r = subprocess.run(
        [sys.executable, "-m", "dlw.cli.seed", "--default"],
        capture_output=True, text=True, timeout=30,
        env=_env_for_subprocess(test_db_name),
    )
    assert r.returncode == 0, r.stderr
    assert "default seed applied" in (r.stdout + r.stderr).lower()

    # Verify via direct session
    count = await db_session.scalar(select(func.count()).select_from(StorageBackend))
    assert count == 1


@pytest.mark.slow
async def test_seed_cli_demo_runs(
    test_db_name: str, db_session: AsyncSession,
) -> None:
    """`dlw-seed --demo` creates the demo DownloadTask."""
    r = subprocess.run(
        [sys.executable, "-m", "dlw.cli.seed", "--demo"],
        capture_output=True, text=True, timeout=30,
        env=_env_for_subprocess(test_db_name),
    )
    assert r.returncode == 0, r.stderr
    assert "demo seed applied" in (r.stdout + r.stderr).lower()

    # The demo task should exist
    task = (await db_session.execute(
        select(DownloadTask).where(DownloadTask.repo_id == "sentence-transformers/all-MiniLM-L6-v2")
    )).scalar_one_or_none()
    assert task is not None
    assert task.status == "pending"
