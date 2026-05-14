"""Tests for require_hmac_heartbeat (Phase 2 W3a §3.4)."""
from __future__ import annotations

import secrets
import time

import pytest
from fastapi import FastAPI, Depends, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.auth.ca import fingerprint_of
from dlw.auth.hmac_heartbeat_dep import require_hmac_heartbeat
from dlw.auth.hmac_nonce import NonceStore, compute_hmac
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


_HMAC_SEED = b"\x02" * 32


async def _seed_executor(engine, executor_id, fp):
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        stmt = pg_insert(Executor).values(
            id=executor_id, host_id="h", cert_fingerprint=fp,
            status="healthy", epoch=1, hmac_seed_encrypted=_HMAC_SEED,
        ).on_conflict_do_update(
            index_elements=["id"],
            set_={"cert_fingerprint": fp, "epoch": 1, "status": "healthy",
                  "hmac_seed_encrypted": _HMAC_SEED},
        )
        await s.execute(stmt)
        await s.commit()


def _mini_app(jwt_keypair):
    app = FastAPI()
    app.state.jwt_keypair = jwt_keypair
    app.state.nonce_store = NonceStore(maxsize=100, ttl_seconds=300)

    @app.post("/hb")
    async def hb(request: Request,
                 ex: Executor = Depends(require_hmac_heartbeat)) -> dict:
        return {"ok": True, "executor_id": ex.id}

    return app


def _hmac_headers(seed, body: bytes, *, ts=None, nonce=None):
    ts = ts if ts is not None else int(time.time())
    nonce = nonce or secrets.token_hex(16)
    sig = compute_hmac(seed, ts=ts, nonce=nonce, body=body)
    return {"X-HMAC-Timestamp": str(ts), "X-HMAC-Nonce": nonce,
            "X-HMAC-Signature": sig}


@pytest.mark.slow
async def test_hmac_heartbeat_accepts_valid_signature(
    engine, ephemeral_ca, client_cert_pair, monkeypatch,
) -> None:
    monkeypatch.setenv("DLW_TLS_TRUSTED_PROXY", "1")
    cert_pem, _key, executor_id = client_cert_pair
    fp = fingerprint_of(cert_pem)
    await _seed_executor(engine, executor_id, fp)
    token = sign(ephemeral_ca["jwt_keypair"], executor_id=executor_id,
                 epoch=1, scopes=["heartbeat"])
    body = b'{"health_score":100}'
    app = _mini_app(ephemeral_ca["jwt_keypair"])
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        r = await c.post("/hb", content=body, headers={
            "X-Client-Cert-PEM": cert_pem.decode("utf-8").replace("\n", "\\n"),
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            **_hmac_headers(_HMAC_SEED, body),
        })
    assert r.status_code == 200


@pytest.mark.slow
async def test_hmac_heartbeat_rejects_clock_skew(
    engine, ephemeral_ca, client_cert_pair, monkeypatch,
) -> None:
    monkeypatch.setenv("DLW_TLS_TRUSTED_PROXY", "1")
    cert_pem, _key, executor_id = client_cert_pair
    fp = fingerprint_of(cert_pem)
    await _seed_executor(engine, executor_id, fp)
    token = sign(ephemeral_ca["jwt_keypair"], executor_id=executor_id,
                 epoch=1, scopes=["heartbeat"])
    body = b'{"health_score":100}'
    app = _mini_app(ephemeral_ca["jwt_keypair"])
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        r = await c.post("/hb", content=body, headers={
            "X-Client-Cert-PEM": cert_pem.decode("utf-8").replace("\n", "\\n"),
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            **_hmac_headers(_HMAC_SEED, body, ts=int(time.time()) - 400),
        })
    assert r.status_code == 401


@pytest.mark.slow
async def test_hmac_heartbeat_rejects_replay(
    engine, ephemeral_ca, client_cert_pair, monkeypatch,
) -> None:
    monkeypatch.setenv("DLW_TLS_TRUSTED_PROXY", "1")
    cert_pem, _key, executor_id = client_cert_pair
    fp = fingerprint_of(cert_pem)
    await _seed_executor(engine, executor_id, fp)
    token = sign(ephemeral_ca["jwt_keypair"], executor_id=executor_id,
                 epoch=1, scopes=["heartbeat"])
    body = b'{"health_score":100}'
    headers_base = {
        "X-Client-Cert-PEM": cert_pem.decode("utf-8").replace("\n", "\\n"),
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    hmac_h = _hmac_headers(_HMAC_SEED, body, nonce="fixed-replay-nonce")
    app = _mini_app(ephemeral_ca["jwt_keypair"])
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        r1 = await c.post("/hb", content=body, headers={**headers_base, **hmac_h})
        r2 = await c.post("/hb", content=body, headers={**headers_base, **hmac_h})
    assert r1.status_code == 200
    assert r2.status_code == 401


@pytest.mark.slow
async def test_hmac_heartbeat_rejects_tampered_body(
    engine, ephemeral_ca, client_cert_pair, monkeypatch,
) -> None:
    monkeypatch.setenv("DLW_TLS_TRUSTED_PROXY", "1")
    cert_pem, _key, executor_id = client_cert_pair
    fp = fingerprint_of(cert_pem)
    await _seed_executor(engine, executor_id, fp)
    token = sign(ephemeral_ca["jwt_keypair"], executor_id=executor_id,
                 epoch=1, scopes=["heartbeat"])
    signed_body = b'{"health_score":100}'
    sent_body = b'{"health_score":999}'
    app = _mini_app(ephemeral_ca["jwt_keypair"])
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        r = await c.post("/hb", content=sent_body, headers={
            "X-Client-Cert-PEM": cert_pem.decode("utf-8").replace("\n", "\\n"),
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            **_hmac_headers(_HMAC_SEED, signed_body),
        })
    assert r.status_code == 401
