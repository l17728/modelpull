"""Tests for require_executor_jwt (Phase 2 W3a §3.4)."""
from __future__ import annotations

import pytest
from fastapi import FastAPI, Depends
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.auth.ca import fingerprint_of
from dlw.auth.executor_jwt_dep import require_executor_jwt
from dlw.auth.jwt_signing import sign
from dlw.db.base import Base
from dlw.db.models.executor import Executor


@pytest.fixture(scope="module", autouse=True)
async def _create_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _mini_app(jwt_keypair):
    app = FastAPI()
    app.state.jwt_keypair = jwt_keypair

    @app.get("/whoami")
    async def whoami(ex: Executor = Depends(require_executor_jwt)) -> dict:
        return {"executor_id": ex.id}

    return app


@pytest.mark.slow
async def test_require_executor_jwt_accepts_valid_token(
    engine, ephemeral_ca, client_cert_pair, monkeypatch,
) -> None:
    monkeypatch.setenv("DLW_TLS_TRUSTED_PROXY", "1")
    cert_pem, _key, executor_id = client_cert_pair
    fp = fingerprint_of(cert_pem)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        stmt = pg_insert(Executor).values(
            id=executor_id, host_id="h", cert_fingerprint=fp,
            status="healthy", epoch=2,
        ).on_conflict_do_update(
            index_elements=["id"],
            set_={"cert_fingerprint": fp, "epoch": 2, "status": "healthy"},
        )
        await s.execute(stmt)
        await s.commit()
    token = sign(ephemeral_ca["jwt_keypair"], executor_id=executor_id,
                 epoch=2, scopes=["heartbeat"])

    app = _mini_app(ephemeral_ca["jwt_keypair"])
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        r = await c.get("/whoami", headers={
            "X-Client-Cert-PEM": cert_pem.decode("utf-8").replace("\n", "\\n"),
            "Authorization": f"Bearer {token}",
        })
    assert r.status_code == 200
    assert r.json()["executor_id"] == executor_id


@pytest.mark.slow
async def test_require_executor_jwt_rejects_sub_mismatch(
    engine, ephemeral_ca, client_cert_pair, monkeypatch,
) -> None:
    monkeypatch.setenv("DLW_TLS_TRUSTED_PROXY", "1")
    cert_pem, _key, executor_id = client_cert_pair
    fp = fingerprint_of(cert_pem)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        stmt = pg_insert(Executor).values(
            id=executor_id, host_id="h", cert_fingerprint=fp,
            status="healthy", epoch=2,
        ).on_conflict_do_update(
            index_elements=["id"],
            set_={"cert_fingerprint": fp, "epoch": 2, "status": "healthy"},
        )
        await s.execute(stmt)
        await s.commit()
    token = sign(ephemeral_ca["jwt_keypair"], executor_id="other-executor",
                 epoch=2, scopes=["heartbeat"])

    app = _mini_app(ephemeral_ca["jwt_keypair"])
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        r = await c.get("/whoami", headers={
            "X-Client-Cert-PEM": cert_pem.decode("utf-8").replace("\n", "\\n"),
            "Authorization": f"Bearer {token}",
        })
    assert r.status_code == 401
