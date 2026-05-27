"""Tests for require_executor_mtls (Phase 2 W3a §3.4)."""
from __future__ import annotations

import pytest
from fastapi import FastAPI, Depends
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.auth.ca import fingerprint_of
from dlw.auth.executor_mtls import require_executor_mtls
from dlw.db.base import Base
from dlw.db.models.executor import Executor


@pytest.fixture(scope="module", autouse=True)
async def _create_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _mini_app():
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(ex: Executor = Depends(require_executor_mtls)) -> dict:
        return {"executor_id": ex.id}

    return app


@pytest.mark.slow
async def test_require_executor_mtls_via_trusted_proxy_header(
    engine, client_cert_pair, monkeypatch,
) -> None:
    monkeypatch.setenv("DLW_TLS_TRUSTED_PROXY", "1")
    cert_pem, _key, executor_id = client_cert_pair
    fp = fingerprint_of(cert_pem)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        stmt = pg_insert(Executor).values(
            id=executor_id, host_id="h", cert_fingerprint=fp,
            status="healthy", epoch=1,
        ).on_conflict_do_update(
            index_elements=["id"],
            set_={"cert_fingerprint": fp, "epoch": 1, "status": "healthy"},
        )
        await s.execute(stmt)
        await s.commit()

    app = _mini_app()
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        r = await c.get("/whoami", headers={
            "X-Client-Cert-PEM": cert_pem.decode("utf-8").replace("\n", "\\n"),
        })
    assert r.status_code == 200
    assert r.json()["executor_id"] == executor_id


@pytest.mark.slow
async def test_require_executor_mtls_rejects_unknown_fingerprint(
    client_cert_pair, monkeypatch,
) -> None:
    monkeypatch.setenv("DLW_TLS_TRUSTED_PROXY", "1")
    cert_pem, _key, _ = client_cert_pair
    app = _mini_app()
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        r = await c.get("/whoami", headers={
            "X-Client-Cert-PEM": cert_pem.decode("utf-8").replace("\n", "\\n"),
        })
    assert r.status_code == 401


@pytest.mark.slow
async def test_require_executor_mtls_rejects_header_when_proxy_disabled(
    client_cert_pair, monkeypatch,
) -> None:
    monkeypatch.setenv("DLW_TLS_TRUSTED_PROXY", "0")
    cert_pem, _key, _ = client_cert_pair
    app = _mini_app()
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        r = await c.get("/whoami", headers={
            "X-Client-Cert-PEM": cert_pem.decode("utf-8").replace("\n", "\\n"),
        })
    assert r.status_code == 401


# --------------------------------------------------------------------------
# DLW_AUTH_DEV_MODE bypass — single-host POC path.

def _devmode_app():
    """Same as _mini_app but the URL carries an /executors/{id}/ segment
    so the dev-mode regex can match. Production endpoints live under
    /api/v1/executors/{id}/{poll,heartbeat,report}; the test uses an
    equivalent shape."""
    app = FastAPI()

    @app.get("/api/v1/executors/{ex_id}/whoami")
    async def whoami(
        ex_id: str, ex: Executor = Depends(require_executor_mtls),
    ) -> dict:
        return {"executor_id": ex.id}

    return app


@pytest.mark.slow
async def test_devmode_bypass_resolves_executor_by_url_path(
    engine, monkeypatch,
) -> None:
    """When DLW_AUTH_DEV_MODE=true and no cert is offered, executor is
    looked up by id in the URL path. This is the single-host POC path
    in deploy/single-host/."""
    monkeypatch.setenv("DLW_AUTH_DEV_MODE", "true")
    monkeypatch.delenv("DLW_TLS_TRUSTED_PROXY", raising=False)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    ex_id = "devmode-bypass-target"
    async with factory() as s:
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        stmt = pg_insert(Executor).values(
            id=ex_id, host_id="h",
            cert_fingerprint="placeholder-no-tls-in-dev-mode",
            status="healthy", epoch=1,
        ).on_conflict_do_update(
            index_elements=["id"],
            set_={"status": "healthy", "epoch": 1},
        )
        await s.execute(stmt)
        await s.commit()

    app = _devmode_app()
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        r = await c.get(f"/api/v1/executors/{ex_id}/whoami")
    assert r.status_code == 200, r.text
    assert r.json()["executor_id"] == ex_id


@pytest.mark.slow
async def test_devmode_bypass_rejects_unknown_executor_id(
    monkeypatch,
) -> None:
    """Dev mode still requires the executor row to exist; an unknown id
    in the URL returns 401 just like the regular cert-mismatch path."""
    monkeypatch.setenv("DLW_AUTH_DEV_MODE", "true")
    monkeypatch.delenv("DLW_TLS_TRUSTED_PROXY", raising=False)
    app = _devmode_app()
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        r = await c.get("/api/v1/executors/never-registered/whoami")
    assert r.status_code == 401


@pytest.mark.slow
async def test_devmode_disabled_still_requires_cert(
    monkeypatch,
) -> None:
    """When DLW_AUTH_DEV_MODE is unset/false, no-cert requests are still
    rejected — the bypass is opt-in."""
    monkeypatch.delenv("DLW_AUTH_DEV_MODE", raising=False)
    monkeypatch.delenv("DLW_TLS_TRUSTED_PROXY", raising=False)
    app = _devmode_app()
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        r = await c.get("/api/v1/executors/anybody/whoami")
    assert r.status_code == 401
