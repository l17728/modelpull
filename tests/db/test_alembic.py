"""Integration test: alembic migration is round-trip-safe.

Runs alembic CLI via subprocess against the per-session test DB so the test
exercises the same code path as a real production migration would.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

REPO_ROOT = Path(__file__).resolve().parents[2]

# Resolve uv executable — on Windows the bash wrapper at ~/bin/uv is a shell
# script that subprocess.run cannot execute directly (WinError 193).  We locate
# the real .exe by following the shebang chain: try Windows %PATH% first via
# shutil.which (works when pytest is launched from a normal Windows shell), then
# fall back to the known WinGet install location.
def _find_uv() -> str:
    # 1. Try the Windows-native PATH (cmd / PowerShell launch environment)
    candidate = shutil.which("uv.exe") or shutil.which("uv")
    if candidate and not candidate.endswith(".exe"):
        # Might be the bash shim — skip it
        try:
            import subprocess as _sp
            r = _sp.run([candidate, "--version"], capture_output=True, timeout=5)
            if r.returncode != 0:
                candidate = None
        except Exception:
            candidate = None
    if candidate:
        return candidate
    # 2. Known WinGet install path
    winget_uv = (
        r"C:\Users\HW\AppData\Local\Microsoft\WinGet\Packages"
        r"\astral-sh.uv_Microsoft.Winget.Source_8wekyb3d8bbwe\uv.exe"
    )
    if Path(winget_uv).exists():
        return winget_uv
    raise RuntimeError("uv.exe not found; install uv or set PATH appropriately")


_UV_BIN: str = _find_uv()

EXPECTED_TABLES = {
    "alembic_version",
    "audit_log",
    "download_tasks",
    "executors",
    "file_subtasks",
    "projects",
    "storage_backends",
    "tenants",
    "users",
}


def _alembic(args: list[str], test_db_name: str) -> None:
    """Run alembic CLI against the per-session test DB.

    Inherits DLW_DB_HOST/PORT/USER/PASSWORD from environment (CI workflow
    sets these to point at the postgres service container; locally they
    fall back to dlw.config defaults of localhost:5433/postgres/trust).
    Only DLW_DB_NAME is overridden — that's the per-session test database.
    """
    env = os.environ.copy()
    # If the runner hasn't set these (local dev), match conftest defaults
    env.setdefault("DLW_DB_HOST", os.environ.get("DLW_TEST_PG_HOST", "localhost"))
    env.setdefault("DLW_DB_PORT", os.environ.get("DLW_TEST_PG_PORT", "5433"))
    env.setdefault("DLW_DB_USER", os.environ.get("DLW_TEST_PG_USER", "postgres"))
    env.setdefault("DLW_DB_PASSWORD", os.environ.get("DLW_TEST_PG_PASSWORD", ""))
    env["DLW_DB_NAME"] = test_db_name  # always the per-session test DB
    result = subprocess.run(
        [_UV_BIN, "run", "alembic", *args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"alembic {args} failed (exit {result.returncode}):\n"
        f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )


@pytest.mark.slow
async def test_upgrade_head_creates_all_tables(
    engine: AsyncEngine, test_db_name: str
) -> None:
    """alembic upgrade head produces the expected 9-table set."""
    _alembic(["upgrade", "head"], test_db_name)
    async with engine.connect() as conn:
        def _get_tables(sync_conn):
            return set(inspect(sync_conn).get_table_names())
        actual = await conn.run_sync(_get_tables)
    assert actual == EXPECTED_TABLES, (
        f"Tables mismatch (XOR diff): {actual ^ EXPECTED_TABLES}"
    )


@pytest.mark.slow
async def test_downgrade_then_reupgrade(
    engine: AsyncEngine, test_db_name: str
) -> None:
    """alembic upgrade head → downgrade base → upgrade head leaves expected set."""
    # First make sure we're at head (idempotent if Task 12 test 1 already ran)
    _alembic(["upgrade", "head"], test_db_name)
    # Roll all the way back to base
    _alembic(["downgrade", "base"], test_db_name)
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
        )
        actual = {row[0] for row in result.all()}
    # After downgrade base only alembic_version remains (it's the bookkeeping table)
    assert actual == {"alembic_version"}, (
        f"Expected only alembic_version after downgrade, got {actual}"
    )
    # Re-upgrade so subsequent tests see the schema
    _alembic(["upgrade", "head"], test_db_name)
