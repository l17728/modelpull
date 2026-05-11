"""Tests for the require_executor_epoch FastAPI dependency."""
from __future__ import annotations

import pytest
from fastapi import FastAPI, Depends
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.config import get_settings
from dlw.db.base import Base
from dlw.db.models.executor import Executor


_TOKEN = "test-bearer-token-12345"


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    """W6-I: do NOT drop_all at module end — multiple modules share this engine.
    Just create_all (idempotent) + seed a probe executor; rely on per-test
    rollback for cleanup of OTHER state.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        # Use ON CONFLICT DO NOTHING so re-running the module is safe
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        stmt = pg_insert(Executor).values(
            id="probe-host-worker-1", host_id="probe-host",
            cert_fingerprint="x", status="healthy", epoch=3,
        ).on_conflict_do_nothing()
        await s.execute(stmt)
        await s.commit()
    yield
    # No drop_all — leave tables for other test modules.


@pytest.fixture(autouse=True)
def _set_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DLW_BEARER_TOKEN", _TOKEN)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def app():
    """Build a tiny app that mounts the dep on a probe endpoint."""
    from dlw.api.tasks import _session
    from dlw.auth.executor_epoch import require_executor_epoch

    app = FastAPI()

    @app.get("/probe/{executor_id}")
    async def probe(executor: Executor = Depends(require_executor_epoch)):
        return {"executor_id": executor.id, "epoch": executor.epoch}

    return app


@pytest.fixture
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest.mark.slow
async def test_require_epoch_missing_header_returns_401(client: AsyncClient) -> None:
    # W6-D: dep accepts Optional + raises 401 with custom detail (not FastAPI auto-422)
    r = await client.get("/probe/probe-host-worker-1")
    assert r.status_code == 401
    assert "missing X-Executor-Epoch" in r.json()["detail"]


@pytest.mark.slow
async def test_require_epoch_unknown_executor_returns_404(
    client: AsyncClient,
) -> None:
    r = await client.get(
        "/probe/no-such-host-worker-99",
        headers={"X-Executor-Epoch": "1"},
    )
    assert r.status_code == 404
    assert "executor not found" in r.json()["detail"]


@pytest.mark.slow
async def test_require_epoch_mismatch_returns_EPOCH_MISMATCH(
    client: AsyncClient,
) -> None:
    r = await client.get(
        "/probe/probe-host-worker-1",
        headers={"X-Executor-Epoch": "2"},
    )
    assert r.status_code == 401
    body = r.json()
    assert body["detail"]["code"] == "EPOCH_MISMATCH"
    assert body["detail"]["expected"] == 3
    assert body["detail"]["got"] == 2


@pytest.mark.slow
async def test_require_epoch_match_returns_executor_row(
    client: AsyncClient,
) -> None:
    r = await client.get(
        "/probe/probe-host-worker-1",
        headers={"X-Executor-Epoch": "3"},
    )
    assert r.status_code == 200
    assert r.json() == {"executor_id": "probe-host-worker-1", "epoch": 3}
