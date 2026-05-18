"""Real-TLS e2e: register → heartbeat full flow (Phase 2 W3a §7.2).

Spawns uvicorn with actual --ssl-* flags and exercises mTLS end-to-end.
This is the single test that verifies the uvicorn TLS wiring + peer-cert
extraction path; all other W3a auth tests use the header-bypass.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
import time

import httpx
import pytest

import dlw.db.models  # noqa: F401  -- register all tables on Base.metadata
from dlw.auth.ca import bootstrap_ca, ensure_server_cert
from dlw.auth.hmac_nonce import compute_hmac
from dlw.db.base import Base


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    """Create tables at module start; drop all at end (mirrors other e2e modules
    so each e2e module gets a clean schema for its seed data)."""
    async with engine.begin() as conn:
        # Clean slate: drop first so a Tenant(id=1) left by another module's
        # bootstrap on the session-scoped DB can't collide with this seed.
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.mark.slow
async def test_register_then_heartbeat_full_flow(tmp_path, engine, test_db_name) -> None:
    """Spawn uvicorn with real TLS; register an executor; send an HMAC-signed
    heartbeat over mTLS. Verifies the uvicorn wiring + peer-cert extraction."""
    # 1. Pre-create the CA + server cert. The subprocess controller's lifespan
    #    calls bootstrap_ca on the same dir — it's idempotent, so it loads these.
    ca_dir = tmp_path / "ca"
    ca_dir.mkdir()
    ca = bootstrap_ca(ca_dir)
    server_cert, server_key = ensure_server_cert(ca, ca_dir, hostname="localhost")

    # 2. Seed the minimal FK rows the subprocess controller needs for startup
    #    and registration. Tables are created by the module-scoped _bootstrap fixture.
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Project, Tenant, User
    from sqlalchemy.ext.asyncio import async_sessionmaker
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(Tenant(id=1, slug="d", display_name="D"))
        await s.flush()
        s.add(Project(id=1, tenant_id=1, name="d"))
        s.add(User(id=1, tenant_id=1, oidc_subject="d",
                   email="d@l", role="tenant_admin"))
        s.add(StorageBackend(id=1, tenant_id=1, name="d",
                              backend_type="s3", config_encrypted=b""))
        await s.commit()

    port = _free_port()
    enrollment_token = "e2e-real-tls-enrollment-token"

    # 3. Spawn uvicorn with real TLS. Plumb the test-DB env vars so the
    #    subprocess controller connects to the SAME test database the
    #    `engine` fixture created.
    env = {
        **os.environ,
        "DLW_CA_DIR": str(ca_dir),
        "DLW_ENROLLMENT_TOKEN": enrollment_token,
        "DLW_CONTROLLER_HOSTNAME": "localhost",
        # SP1 fail-closed startup guard: this e2e only exercises executor
        # mTLS/heartbeat (no user OIDC), so dev mode is the right bypass.
        "DLW_AUTH_DEV_MODE": "true",
        "DLW_DB_HOST": os.environ.get("DLW_TEST_PG_HOST", "localhost"),
        "DLW_DB_PORT": os.environ.get("DLW_TEST_PG_PORT", "5433"),
        "DLW_DB_USER": os.environ.get("DLW_TEST_PG_USER", "postgres"),
        "DLW_DB_PASSWORD": os.environ.get("DLW_TEST_PG_PASSWORD", ""),
        "DLW_DB_NAME": test_db_name,
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "dlw.main:app",
         "--host", "127.0.0.1", "--port", str(port),
         "--ssl-keyfile", str(server_key),
         "--ssl-certfile", str(server_cert),
         "--ssl-ca-certs", str(ca_dir / "ca-cert.pem"),
         "--ssl-cert-reqs", "1"],  # CERT_OPTIONAL: app layer enforces presence
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        base = f"https://localhost:{port}"
        ca_cert_path = str(ca_dir / "ca-cert.pem")

        # 4. Wait for the server to be ready. /healthz may or may not require
        #    mTLS — probe with the CA bundle; tolerate any non-connection error.
        ready = False
        for _ in range(60):
            if proc.poll() is not None:
                out = proc.stdout.read().decode() if proc.stdout else ""
                pytest.fail(f"uvicorn exited early (code {proc.returncode}):\n{out}")
            try:
                async with httpx.AsyncClient(verify=ca_cert_path) as c:
                    r = await c.get(f"{base}/health/live", timeout=1.0)
                    if r.status_code < 500:
                        ready = True
                        break
            except Exception:
                await asyncio.sleep(0.3)
        if not ready:
            out = proc.stdout.read().decode() if proc.stdout else ""
            pytest.fail(f"uvicorn did not become ready:\n{out}")

        # 5. Build a CSR + register (no client cert for /register — enrollment token).
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        key = ed25519.Ed25519PrivateKey.generate()
        csr = (x509.CertificateSigningRequestBuilder()
            .subject_name(x509.Name([
                x509.NameAttribute(NameOID.COMMON_NAME, "e2e-tls-worker-1"),
            ]))
            .sign(key, None))
        csr_pem = csr.public_bytes(serialization.Encoding.PEM).decode()

        async with httpx.AsyncClient(verify=ca_cert_path) as c:
            reg = await c.post(f"{base}/api/v1/executors/register", json={
                "host_id": "e2e-tls-host",
                "executor_id_proposal": "e2e-tls-worker-1",
                "capabilities": {}, "client_csr_pem": csr_pem,
            }, headers={"X-Enrollment-Token": enrollment_token})
        assert reg.status_code == 201, reg.text
        body = reg.json()

        # 6. Persist the issued client cert + key for the mTLS heartbeat call.
        client_cert_path = tmp_path / "client-cert.pem"
        client_key_path = tmp_path / "client-key.pem"
        client_cert_path.write_text(body["client_cert_pem"])
        client_key_path.write_bytes(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ))

        # 7. Heartbeat over real mTLS + JWT + HMAC.
        hb_body = json.dumps({"health_score": 100, "parts_dir_bytes": 0}).encode()
        ts = int(time.time())
        nonce = secrets.token_hex(16)
        sig = compute_hmac(bytes.fromhex(body["hmac_seed_hex"]),
                           ts=ts, nonce=nonce, body=hb_body)
        async with httpx.AsyncClient(
            verify=ca_cert_path,
            cert=(str(client_cert_path), str(client_key_path)),
        ) as c:
            hb = await c.post(
                f"{base}/api/v1/executors/e2e-tls-worker-1/heartbeat",
                content=hb_body,
                headers={
                    "Authorization": f"Bearer {body['executor_jwt']}",
                    "X-Executor-Epoch": str(body["epoch"]),
                    "X-HMAC-Timestamp": str(ts),
                    "X-HMAC-Nonce": nonce,
                    "X-HMAC-Signature": sig,
                    "Content-Type": "application/json",
                },
            )
        assert hb.status_code == 200, hb.text
        assert hb.json()["status"] == "healthy"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
