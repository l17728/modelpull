"""Tests for POST /api/v1/executors/{eid}/renew (Phase 2 W3a §3.5)."""
from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography import x509
from cryptography.x509.oid import NameOID
from httpx import ASGITransport, AsyncClient

import dlw.db.models  # noqa: F401 — registers all ORM models with Base.metadata
from dlw.db.base import Base


_ENROLL = "test-enrollment-token-w3a-renew"


def _build_csr(executor_id: str) -> str:
    key = ed25519.Ed25519PrivateKey.generate()
    csr = (x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, executor_id)]))
        .sign(key, None))
    return csr.public_bytes(serialization.Encoding.PEM).decode("utf-8")


@pytest.fixture(scope="module", autouse=True)
async def _create_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def client(ephemeral_ca, monkeypatch):
    from tests.conftest import make_app_with_state
    monkeypatch.setenv("DLW_TLS_TRUSTED_PROXY", "1")
    app = make_app_with_state(ephemeral_ca, enrollment_token=_ENROLL)
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c


async def _register(client, executor_id: str) -> dict:
    csr = _build_csr(executor_id)
    r = await client.post("/api/v1/executors/register", json={
        "host_id": "renew-host", "executor_id_proposal": executor_id,
        "capabilities": {}, "client_csr_pem": csr,
    }, headers={"X-Enrollment-Token": _ENROLL})
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.slow
async def test_renew_returns_new_jwt_only_when_no_csr(client) -> None:
    reg = await _register(client, "renew-worker-1")
    r = await client.post(
        "/api/v1/executors/renew-worker-1/renew", json={},
        headers={
            "X-Client-Cert-PEM": reg["client_cert_pem"].replace("\n", "\\n"),
            "Authorization": f"Bearer {reg['executor_jwt']}",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["executor_jwt"]
    assert body["client_cert_pem"] is None


@pytest.mark.slow
async def test_renew_returns_new_cert_when_csr_provided(client) -> None:
    reg = await _register(client, "renew-worker-2")
    fresh_csr = _build_csr("renew-worker-2")
    r = await client.post(
        "/api/v1/executors/renew-worker-2/renew",
        json={"client_csr_pem": fresh_csr},
        headers={
            "X-Client-Cert-PEM": reg["client_cert_pem"].replace("\n", "\\n"),
            "Authorization": f"Bearer {reg['executor_jwt']}",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["executor_jwt"]
    assert body["client_cert_pem"] is not None
    assert body["client_cert_pem"].startswith("-----BEGIN CERTIFICATE-----")
