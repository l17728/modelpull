"""Tests for the require_executor_epoch FastAPI dependency."""
from __future__ import annotations

import pytest
from fastapi import FastAPI, Depends, Path
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.auth.ca import fingerprint_of
from dlw.auth.jwt_signing import sign
from dlw.db.base import Base
from dlw.db.models.executor import Executor


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    """Create tables once per module; drop at end."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _build_app(jwt_keypair):
    """Build a tiny app that mounts require_executor_epoch on a probe endpoint."""
    from dlw.auth.executor_epoch import require_executor_epoch

    app = FastAPI()
    app.state.jwt_keypair = jwt_keypair

    @app.post("/probe/{executor_id}")
    async def probe(executor_id: str = Path(...),
                    executor: Executor = Depends(require_executor_epoch)):
        return {"executor_id": executor.id, "epoch": executor.epoch}

    return app


async def _seed_probe_executor(engine, executor_id, cert_fingerprint, epoch=3):
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        stmt = pg_insert(Executor).values(
            id=executor_id, host_id="probe-host",
            cert_fingerprint=cert_fingerprint, status="healthy", epoch=epoch,
        ).on_conflict_do_update(
            index_elements=["id"],
            set_={"cert_fingerprint": cert_fingerprint, "epoch": epoch,
                  "status": "healthy"},
        )
        await s.execute(stmt)
        await s.commit()


@pytest.mark.slow
async def test_require_epoch_missing_header_returns_401(
    engine, ephemeral_ca, client_cert_pair, monkeypatch,
) -> None:
    """Missing X-Executor-Epoch → 401 (mTLS+JWT pass, epoch header absent)."""
    monkeypatch.setenv("DLW_TLS_TRUSTED_PROXY", "1")
    cert_pem, _key, executor_id = client_cert_pair
    fp = fingerprint_of(cert_pem)
    await _seed_probe_executor(engine, executor_id, fp, epoch=3)
    token = sign(ephemeral_ca["jwt_keypair"], executor_id=executor_id,
                 epoch=3, scopes=["heartbeat"])
    app = _build_app(ephemeral_ca["jwt_keypair"])
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        r = await c.post(f"/probe/{executor_id}", headers={
            "X-Client-Cert-PEM": cert_pem.decode("utf-8").replace("\n", "\\n"),
            "Authorization": f"Bearer {token}",
        })
    assert r.status_code == 401
    assert "missing X-Executor-Epoch" in r.json()["detail"]


@pytest.mark.slow
async def test_require_epoch_unknown_executor_returns_executor_id_mismatch(
    engine, ephemeral_ca, client_cert_pair, monkeypatch,
) -> None:
    """Path executor_id differs from mTLS-authenticated identity → 401 EXECUTOR_ID_MISMATCH.

    W3a refactor: confused-deputy guard fires before epoch check.
    (Previously returned 404 'executor not found'; that DB lookup is removed.)
    """
    monkeypatch.setenv("DLW_TLS_TRUSTED_PROXY", "1")
    cert_pem, _key, executor_id = client_cert_pair
    fp = fingerprint_of(cert_pem)
    await _seed_probe_executor(engine, executor_id, fp, epoch=3)
    token = sign(ephemeral_ca["jwt_keypair"], executor_id=executor_id,
                 epoch=3, scopes=["heartbeat"])
    app = _build_app(ephemeral_ca["jwt_keypair"])
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        r = await c.post("/probe/no-such-host-worker-99", headers={
            "X-Client-Cert-PEM": cert_pem.decode("utf-8").replace("\n", "\\n"),
            "Authorization": f"Bearer {token}",
            "X-Executor-Epoch": "1",
        })
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "EXECUTOR_ID_MISMATCH"


@pytest.mark.slow
async def test_require_epoch_mismatch_returns_EPOCH_MISMATCH(
    engine, ephemeral_ca, client_cert_pair, monkeypatch,
) -> None:
    monkeypatch.setenv("DLW_TLS_TRUSTED_PROXY", "1")
    cert_pem, _key, executor_id = client_cert_pair
    fp = fingerprint_of(cert_pem)
    await _seed_probe_executor(engine, executor_id, fp, epoch=3)
    token = sign(ephemeral_ca["jwt_keypair"], executor_id=executor_id,
                 epoch=3, scopes=["heartbeat"])
    app = _build_app(ephemeral_ca["jwt_keypair"])
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        r = await c.post(f"/probe/{executor_id}", headers={
            "X-Client-Cert-PEM": cert_pem.decode("utf-8").replace("\n", "\\n"),
            "Authorization": f"Bearer {token}",
            "X-Executor-Epoch": "2",
        })
    assert r.status_code == 401
    body = r.json()
    assert body["detail"]["code"] == "EPOCH_MISMATCH"
    assert body["detail"]["expected"] == 3
    assert body["detail"]["got"] == 2


@pytest.mark.slow
async def test_require_epoch_match_returns_executor_row(
    engine, ephemeral_ca, client_cert_pair, monkeypatch,
) -> None:
    monkeypatch.setenv("DLW_TLS_TRUSTED_PROXY", "1")
    cert_pem, _key, executor_id = client_cert_pair
    fp = fingerprint_of(cert_pem)
    await _seed_probe_executor(engine, executor_id, fp, epoch=3)
    token = sign(ephemeral_ca["jwt_keypair"], executor_id=executor_id,
                 epoch=3, scopes=["heartbeat"])
    app = _build_app(ephemeral_ca["jwt_keypair"])
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        r = await c.post(f"/probe/{executor_id}", headers={
            "X-Client-Cert-PEM": cert_pem.decode("utf-8").replace("\n", "\\n"),
            "Authorization": f"Bearer {token}",
            "X-Executor-Epoch": "3",
        })
    assert r.status_code == 200
    assert r.json() == {"executor_id": executor_id, "epoch": 3}


@pytest.mark.slow
async def test_require_executor_epoch_rejects_path_id_mismatch(
    engine, ephemeral_ca, client_cert_pair, monkeypatch,
) -> None:
    """mTLS+JWT authenticate executor A, URL path says executor B →
    401 EXECUTOR_ID_MISMATCH (confused-deputy guard)."""
    from dlw.auth.executor_epoch import require_executor_epoch

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setenv("DLW_TLS_TRUSTED_PROXY", "1")
    cert_pem, _key, executor_id = client_cert_pair
    fp = fingerprint_of(cert_pem)
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        stmt = pg_insert(Executor).values(
            id=executor_id, host_id="h", cert_fingerprint=fp,
            status="healthy", epoch=1,
        ).on_conflict_do_update(
            index_elements=["id"],
            set_={"cert_fingerprint": fp, "epoch": 1, "status": "healthy"},
        )
        await s.execute(stmt)
        await s.commit()
    token = sign(ephemeral_ca["jwt_keypair"], executor_id=executor_id,
                 epoch=1, scopes=["heartbeat"])

    app = FastAPI()
    app.state.jwt_keypair = ephemeral_ca["jwt_keypair"]

    @app.post("/executors/{executor_id}/x")
    async def x(executor_id: str = Path(...),
                ex: Executor = Depends(require_executor_epoch)) -> dict:
        return {"ok": True}

    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        r = await c.post("/executors/other-executor/x", headers={
            "X-Client-Cert-PEM": cert_pem.decode("utf-8").replace("\n", "\\n"),
            "Authorization": f"Bearer {token}",
            "X-Executor-Epoch": "1",
        })
    assert r.status_code == 401
