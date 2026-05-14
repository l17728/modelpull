"""Tests for POST /api/v1/executors/register (Phase 2 W3a §3.5)."""
from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography import x509
from cryptography.x509.oid import NameOID
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

import dlw.db.models  # noqa: F401 — registers all ORM models with Base.metadata
from dlw.db.base import Base


_ENROLL = "test-enrollment-token-w3a"


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
async def client(ephemeral_ca, monkeypatch, tmp_path):
    """App with W3a auth state set directly on app.state (skip the real
    lifespan bootstrap — set ca/jwt_keypair/enrollment_token from ephemeral_ca)."""
    from dlw.main import create_app
    from dlw.auth.hmac_nonce import NonceStore
    app = create_app()
    app.state.ca = ephemeral_ca["ca"]
    app.state.jwt_keypair = ephemeral_ca["jwt_keypair"]
    app.state.nonce_store = NonceStore(maxsize=100, ttl_seconds=300)
    app.state.enrollment_token = _ENROLL
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c


@pytest.mark.slow
async def test_register_returns_cert_jwt_and_hmac_seed(client) -> None:
    csr = _build_csr("reg-host-worker-1")
    r = await client.post("/api/v1/executors/register", json={
        "host_id": "reg-host", "executor_id_proposal": "reg-host-worker-1",
        "capabilities": {}, "client_csr_pem": csr,
    }, headers={"X-Enrollment-Token": _ENROLL})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["executor_id"] == "reg-host-worker-1"
    assert body["epoch"] == 1
    assert body["client_cert_pem"].startswith("-----BEGIN CERTIFICATE-----")
    assert body["executor_jwt"]
    assert len(bytes.fromhex(body["hmac_seed_hex"])) == 32
    assert body["ca_chain"]


@pytest.mark.slow
async def test_register_rejects_invalid_enrollment_token(client) -> None:
    csr = _build_csr("reg-host-worker-2")
    r = await client.post("/api/v1/executors/register", json={
        "host_id": "reg-host", "executor_id_proposal": "reg-host-worker-2",
        "capabilities": {}, "client_csr_pem": csr,
    }, headers={"X-Enrollment-Token": "wrong-token"})
    assert r.status_code == 401


@pytest.mark.slow
async def test_register_idempotent_on_reregister(client) -> None:
    csr1 = _build_csr("reg-host-worker-3")
    r1 = await client.post("/api/v1/executors/register", json={
        "host_id": "reg-host", "executor_id_proposal": "reg-host-worker-3",
        "capabilities": {}, "client_csr_pem": csr1,
    }, headers={"X-Enrollment-Token": _ENROLL})
    assert r1.status_code == 201
    epoch1 = r1.json()["epoch"]
    csr2 = _build_csr("reg-host-worker-3")
    r2 = await client.post("/api/v1/executors/register", json={
        "host_id": "reg-host", "executor_id_proposal": "reg-host-worker-3",
        "capabilities": {}, "client_csr_pem": csr2,
    }, headers={"X-Enrollment-Token": _ENROLL})
    assert r2.status_code == 201
    assert r2.json()["epoch"] == epoch1 + 1   # epoch bumped on re-register
