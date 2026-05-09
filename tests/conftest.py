"""Pytest fixtures: session-scoped local PostgreSQL test database.

Uses an existing PostgreSQL instance (default: localhost:5433, postgres user,
trust auth) instead of testcontainers — Docker is optional for development.

Override via env vars:
    DLW_TEST_PG_HOST     (default: localhost)
    DLW_TEST_PG_PORT     (default: 5433)
    DLW_TEST_PG_USER     (default: postgres)
    DLW_TEST_PG_PASSWORD (default: empty)
    DLW_TEST_PG_ADMIN_DB (default: postgres — used to CREATE/DROP test DB)
"""
from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def _pg_env() -> dict[str, str]:
    return {
        "host": os.environ.get("DLW_TEST_PG_HOST", "localhost"),
        "port": os.environ.get("DLW_TEST_PG_PORT", "5433"),
        "user": os.environ.get("DLW_TEST_PG_USER", "postgres"),
        "password": os.environ.get("DLW_TEST_PG_PASSWORD", ""),
        "admin_db": os.environ.get("DLW_TEST_PG_ADMIN_DB", "postgres"),
    }


def _admin_url(env: dict[str, str]) -> str:
    auth = f"{env['user']}:{env['password']}@" if env["password"] else f"{env['user']}@"
    return f"postgresql+asyncpg://{auth}{env['host']}:{env['port']}/{env['admin_db']}"


def _test_url(env: dict[str, str], db_name: str) -> str:
    auth = f"{env['user']}:{env['password']}@" if env["password"] else f"{env['user']}@"
    return f"postgresql+asyncpg://{auth}{env['host']}:{env['port']}/{db_name}"


@pytest.fixture(scope="session")
def event_loop():
    """Session-scoped event loop so per-session async fixtures work.

    pytest-asyncio 0.24 deprecates this override but the alternatives
    (loop_scope="session" + asyncio_default_fixture_loop_scope="session")
    cause "Event loop is closed" errors when per-test async sessions try to
    reuse the session-scoped engine. Suppressed via filterwarnings in pyproject.
    """
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
def test_db_name() -> str:
    """Unique database name per pytest session for isolation."""
    return f"test_dlw_{uuid.uuid4().hex[:12]}"


@pytest_asyncio.fixture(scope="session")
async def engine(test_db_name: str) -> AsyncIterator[AsyncEngine]:
    """Async engine connected to a freshly-created per-session test DB.

    1. Connect to admin DB (postgres) and CREATE DATABASE test_dlw_xxx
    2. Yield engine pointing at the new DB
    3. After session: dispose engine, then DROP DATABASE
    """
    env = _pg_env()
    admin_engine = create_async_engine(
        _admin_url(env), isolation_level="AUTOCOMMIT", echo=False
    )
    try:
        async with admin_engine.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{test_db_name}"'))
    finally:
        await admin_engine.dispose()

    test_engine = create_async_engine(_test_url(env, test_db_name), echo=False, pool_pre_ping=True)
    try:
        yield test_engine
    finally:
        await test_engine.dispose()
        # Drop the test DB (terminate any leftover connections first)
        admin_engine = create_async_engine(
            _admin_url(env), isolation_level="AUTOCOMMIT", echo=False
        )
        try:
            async with admin_engine.connect() as conn:
                await conn.execute(text(
                    f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    f"WHERE datname = '{test_db_name}' AND pid <> pg_backend_pid()"
                ))
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{test_db_name}"'))
        finally:
            await admin_engine.dispose()


@pytest_asyncio.fixture
async def db_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Per-test async session, rolled back after each test for isolation."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
        await session.rollback()


@pytest.fixture(autouse=True)
def _patch_hf_global(monkeypatch: pytest.MonkeyPatch):
    """Global default: HF returns 2 files so POST /tasks succeeds in all test modules.

    Per-test overrides (e.g. to raise RepoNotFound) shadow this via a second
    monkeypatch.setattr in the test body — monkeypatch stacks last-wins within
    the same test.
    """
    from dlw.services.hf_metadata import RepoFile

    async def fake(*args, **kwargs):
        return [
            RepoFile(path="config.json", size=4096, sha256=None),
            RepoFile(path="model.safetensors", size=64 * 1024, sha256=None),
        ]
    monkeypatch.setattr("dlw.services.task_service.list_repo_tree", fake)


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _point_app_at_test_db(test_db_name: str, engine: AsyncEngine):
    """Make the FastAPI app's get_engine() / get_settings() see the test DB.

    Runs once per pytest session AFTER the engine fixture creates the test DB.
    Without this, API tests would query the production `dlw` DB which has no
    seeded fixtures.
    """
    env_overrides = {
        "DLW_DB_HOST": os.environ.get("DLW_TEST_PG_HOST", "localhost"),
        "DLW_DB_PORT": os.environ.get("DLW_TEST_PG_PORT", "5433"),
        "DLW_DB_USER": os.environ.get("DLW_TEST_PG_USER", "postgres"),
        "DLW_DB_PASSWORD": os.environ.get("DLW_TEST_PG_PASSWORD", ""),
        "DLW_DB_NAME": test_db_name,
    }
    saved = {k: os.environ.get(k) for k in env_overrides}
    os.environ.update(env_overrides)

    from dlw.config import get_settings
    from dlw.db.session import reset_engine
    get_settings.cache_clear()
    await reset_engine()

    yield

    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    get_settings.cache_clear()
    await reset_engine()
