"""Tests for LeaderElector (Phase 2 W3c — PG advisory lock primitive)."""
from __future__ import annotations

import pytest

from dlw.services.leader_election import LeaderElector


def _db_url() -> str:
    """Test DB URL — matches the conftest engine fixture."""
    import os
    env = {
        "host": os.environ.get("DLW_TEST_PG_HOST", "localhost"),
        "port": os.environ.get("DLW_TEST_PG_PORT", "5433"),
        "user": os.environ.get("DLW_TEST_PG_USER", "postgres"),
        "password": os.environ.get("DLW_TEST_PG_PASSWORD", ""),
    }
    auth = f"{env['user']}:{env['password']}@" if env["password"] else f"{env['user']}@"
    return f"postgresql+asyncpg://{auth}{env['host']}:{env['port']}/postgres"


_LOCK_ID = 0x4C45_4145_4445_5231  # 'LEAD ER1'


@pytest.mark.slow
async def test_first_acquire_succeeds() -> None:
    """A fresh elector acquires the advisory lock."""
    e = LeaderElector(_db_url(), _LOCK_ID)
    try:
        assert await e.try_acquire() is True
    finally:
        await e.release()


@pytest.mark.slow
async def test_second_elector_cannot_acquire() -> None:
    """While elector A holds the lock, elector B (same lock_id) gets False."""
    a = LeaderElector(_db_url(), _LOCK_ID)
    b = LeaderElector(_db_url(), _LOCK_ID)
    try:
        assert await a.try_acquire() is True
        assert await b.try_acquire() is False
    finally:
        await b.release()
        await a.release()


@pytest.mark.slow
async def test_release_frees_lock_for_next_elector() -> None:
    """After A releases, B can acquire."""
    a = LeaderElector(_db_url(), _LOCK_ID)
    b = LeaderElector(_db_url(), _LOCK_ID)
    try:
        assert await a.try_acquire() is True
        await a.release()
        assert await b.try_acquire() is True
    finally:
        await b.release()


@pytest.mark.slow
async def test_connection_drop_frees_lock() -> None:
    """The crash-failover guarantee: if A's lock-holding connection is closed
    without an explicit unlock, PG releases the lock and B can acquire."""
    a = LeaderElector(_db_url(), _LOCK_ID)
    b = LeaderElector(_db_url(), _LOCK_ID)
    try:
        assert await a.try_acquire() is True
        # Simulate process death by closing the underlying connection directly,
        # bypassing release()'s polite pg_advisory_unlock.
        assert a._conn is not None
        await a._conn.close()
        a._conn = None
        # PG releases on session end; B should now acquire.
        assert await b.try_acquire() is True
    finally:
        await b.release()
        await a.release()


@pytest.mark.slow
async def test_verify_returns_true_when_holding() -> None:
    """verify() pings the connection and confirms it's alive."""
    e = LeaderElector(_db_url(), _LOCK_ID)
    try:
        assert await e.try_acquire() is True
        assert await e.verify() is True
    finally:
        await e.release()


@pytest.mark.slow
async def test_verify_returns_false_after_connection_drop() -> None:
    """verify() returns False if the lock connection has died, and cleans up
    so the next try_acquire() opens a fresh connection."""
    e = LeaderElector(_db_url(), _LOCK_ID)
    try:
        assert await e.try_acquire() is True
        assert e._conn is not None
        await e._conn.close()
        e._conn = None
        assert await e.verify() is False
    finally:
        await e.release()
