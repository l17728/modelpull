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


@pytest.fixture(scope="session")
def ephemeral_ca(tmp_path_factory):
    """One CA + JWT keypair per test session, in a temp dir."""
    from dlw.auth.ca import bootstrap_ca
    from dlw.auth.jwt_signing import bootstrap_keypair
    ca_dir = tmp_path_factory.mktemp("ca")
    ca = bootstrap_ca(ca_dir)
    jwt_kp = bootstrap_keypair(ca_dir)
    return {"ca": ca, "jwt_keypair": jwt_kp, "ca_dir": ca_dir}


@pytest.fixture
def client_cert_pair(ephemeral_ca):
    """Per-test client cert ('test-executor-1') signed by the session CA.
    Returns (cert_pem: bytes, key: Ed25519PrivateKey, executor_id: str)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.x509.oid import NameOID

    from dlw.auth.ca import sign_csr
    executor_id = "test-executor-1"
    key = ed25519.Ed25519PrivateKey.generate()
    csr = (x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, executor_id)]))
        .sign(key, None))
    csr_pem = csr.public_bytes(serialization.Encoding.PEM)
    cert_pem = sign_csr(ephemeral_ca["ca"], csr_pem, executor_id, ttl_hours=24)
    return cert_pem, key, executor_id


# --- W3a: helpers for executor auth in API tests (mTLS+JWT+HMAC via header bypass) ---

async def register_test_executor(
    client, *, enrollment_token: str,
    executor_id: str = "test-host-worker-1", host_id: str = "test-host",
) -> dict:
    """Build a CSR, POST /api/v1/executors/register, return a dict with
    executor_id, epoch, cert_pem, jwt, hmac_seed, ca_chain. The caller's app
    must have app.state.ca / jwt_keypair / nonce_store / enrollment_token set."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.x509.oid import NameOID
    key = ed25519.Ed25519PrivateKey.generate()
    csr = (x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, executor_id)]))
        .sign(key, None))
    csr_pem = csr.public_bytes(serialization.Encoding.PEM).decode("utf-8")
    r = await client.post("/api/v1/executors/register", json={
        "host_id": host_id, "executor_id_proposal": executor_id,
        "capabilities": {}, "client_csr_pem": csr_pem,
    }, headers={"X-Enrollment-Token": enrollment_token})
    assert r.status_code in (200, 201), r.text
    body = r.json()
    return {
        "executor_id": body["executor_id"], "epoch": body["epoch"],
        "cert_pem": body["client_cert_pem"], "jwt": body["executor_jwt"],
        "hmac_seed": bytes.fromhex(body["hmac_seed_hex"]),
        "ca_chain": body["ca_chain"],
    }


def executor_request_headers(reg: dict) -> dict[str, str]:
    """mTLS-bypass + JWT + epoch headers for poll / report (no HMAC)."""
    return {
        "X-Client-Cert-PEM": reg["cert_pem"].replace("\n", "\n"),
        "Authorization": f"Bearer {reg['jwt']}",
        "X-Executor-Epoch": str(reg["epoch"]),
    }


def signed_heartbeat_headers(reg: dict, body: bytes) -> dict[str, str]:
    """mTLS-bypass + JWT + epoch + HMAC headers for a heartbeat body."""
    import secrets as _s
    import time as _t

    from dlw.auth.hmac_nonce import compute_hmac
    ts = int(_t.time())
    nonce = _s.token_hex(16)
    sig = compute_hmac(reg["hmac_seed"], ts=ts, nonce=nonce, body=body)
    return {
        **executor_request_headers(reg),
        "X-HMAC-Timestamp": str(ts),
        "X-HMAC-Nonce": nonce,
        "X-HMAC-Signature": sig,
        "Content-Type": "application/json",
    }


def make_fake_auth_state(
    cert_dir, *, executor_id: str = "test-worker-1", epoch: int = 1,
    jwt: str = "fake.jwt.token", hmac_seed: bytes = b"\x09" * 32,
):
    """Build a minimal AuthState for executor-side tests that inject a
    MockTransport (cert files are never read in that mode)."""
    import datetime as _dt
    from pathlib import Path as _Path

    from dlw.executor.auth_lifecycle import AuthState
    far = _dt.datetime.now(_dt.UTC) + _dt.timedelta(hours=24)
    return AuthState(
        executor_id=executor_id, epoch=epoch,
        cert_pem=b"-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----",
        key_pem=b"-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----",
        ca_chain_pem=b"-----BEGIN CERTIFICATE-----\nfakeca\n-----END CERTIFICATE-----",
        jwt=jwt, jwt_exp=far, cert_exp=far,
        hmac_seed=hmac_seed, cert_dir=_Path(str(cert_dir)),
    )


def make_fake_controller_client(hf_handler):
    """W3b test double for ControllerClient — its stream_hf() routes through an
    httpx.MockTransport(hf_handler) instead of a real controller proxy. Lets
    downloader tests simulate HF responses without a running controller.
    hf_handler is a Callable[[httpx.Request], httpx.Response]."""
    from contextlib import asynccontextmanager as _acm

    import httpx as _httpx

    class _FakeControllerClient:
        def __init__(self):
            self._transport = _httpx.MockTransport(hf_handler)

        @_acm
        async def stream_hf(self, *, subtask_id, assignment_token,
                            range_header=None):
            headers = {"X-Assignment-Token": str(assignment_token)}
            if range_header:
                headers["Range"] = range_header
            async with _httpx.AsyncClient(
                transport=self._transport, base_url="http://fake-controller",
            ) as client:
                async with client.stream(
                    "GET", f"/api/v1/hf-proxy/subtask/{subtask_id}",
                    headers=headers,
                ) as resp:
                    yield resp

        @_acm
        async def stream_source(self, *, subtask_id, assignment_token,
                                range_header=None):
            headers = {"X-Assignment-Token": str(assignment_token)}
            if range_header:
                headers["Range"] = range_header
            async with _httpx.AsyncClient(
                transport=self._transport, base_url="http://fake-controller",
            ) as client:
                async with client.stream(
                    "GET", f"/api/v1/source-proxy/subtask/{subtask_id}",
                    headers=headers,
                ) as resp:
                    yield resp

    return _FakeControllerClient()


def principal_headers(*, user_id: int = 1, tenant_id: int = 1,
                       role: str = "tenant_operator",
                       project_ids: list[int] | None = None,
                       secret: str = "unit-secret") -> dict[str, str]:
    """Authorization header carrying a freshly minted system-JWT (Phase 3 SP1).
    Caller must have set DLW_SYSTEM_JWT_SECRET=secret + cleared the cache."""
    from dlw.auth.principal import issue_system_jwt
    tok = issue_system_jwt(secret=secret, user_id=user_id,
                           tenant_id=tenant_id, role=role,
                           project_ids=project_ids or [])
    return {"Authorization": f"Bearer {tok}"}


def service_headers(token: str = "svc-tok") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def make_app_with_state(
    ephemeral_ca,
    *,
    enrollment_token: str,
    controller_state: str = "active",
):
    """Build a controller app for ASGI-transport tests with app.state pre-seeded
    (skips the lifespan bootstrap). Defaults controller_state to 'active' so
    the W3c recovery barrier doesn't fire."""
    from dlw.auth.hmac_nonce import NonceStore
    from dlw.main import create_app
    app = create_app()
    from dlw.config import get_settings as _gs
    app.state.settings = _gs()
    from dlw.authz.enforcer import build_enforcer
    app.state.casbin = build_enforcer(grants=[])
    from dlw.sources.name_resolver import NameResolver
    from dlw.sources.registry import load_registry
    _s2 = app.state.settings
    app.state.source_registry = load_registry(
        _s2.sources_yaml_path, hf_token=_s2.hf_token)
    app.state.name_resolver = NameResolver.from_file(_s2.resolver_rules_path)
    app.state.ca = ephemeral_ca["ca"]
    app.state.jwt_keypair = ephemeral_ca["jwt_keypair"]
    app.state.nonce_store = NonceStore(maxsize=1000, ttl_seconds=300)
    app.state.enrollment_token = enrollment_token
    app.state.controller_state = controller_state
    return app
