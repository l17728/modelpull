"""Manual smoke: real HF (small public model) → local minio binary.

Skipped on CI (marker `manual`). Run locally with:
    uv run pytest tests/e2e/test_hf_s3_smoke_local.py -m manual -v

Requires:
    - `minio` binary in PATH (https://min.io/download)
    - Network access to huggingface.co
    - PG running on localhost:5433 (same as other tests)

Model: sentence-transformers/all-MiniLM-L6-v2 (~90MB, public, multi-file).
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.config import get_settings
from dlw.db.base import Base
from dlw.executor.auth_lifecycle import AuthState
from dlw.executor.client import ControllerClient
from dlw.executor.config import ExecutorSettings
from dlw.executor.downloader import HfS3StreamDownloader
from dlw.executor.runner import ExecutorRunner
from tests.conftest import (
    make_app_with_state,
    principal_headers,
    register_test_executor,
)

SECRET = "unit-secret"
_TOKEN = "smoke-token"
_ENROLL = "e"
_BUCKET = "smoke-bucket"
_REPO = "sentence-transformers/all-MiniLM-L6-v2"
# Pin to avoid drift; bump when model upstream changes.
_REVISION = "main"


def _wait_for_port(host: str, port: int, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except OSError:
            time.sleep(0.5)
    raise TimeoutError(f"{host}:{port} did not open within {timeout}s")


@pytest.fixture
def minio_proc(tmp_path: Path):
    if shutil.which("minio") is None:
        pytest.skip("minio binary not in PATH — install from https://min.io/download")
    data_dir = tmp_path / "minio-data"
    data_dir.mkdir()
    env = {
        **os.environ,
        "MINIO_ROOT_USER": "minioadmin",
        "MINIO_ROOT_PASSWORD": "minioadmin",
    }
    proc = subprocess.Popen(
        ["minio", "server", str(data_dir), "--address", ":9000"],
        env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_port("localhost", 9000, timeout=15.0)
        s3 = boto3.client(
            "s3",
            endpoint_url="http://localhost:9000",
            aws_access_key_id="minioadmin",
            aws_secret_access_key="minioadmin",
            region_name="us-east-1",
        )
        s3.create_bucket(Bucket=_BUCKET)
        yield s3
    finally:
        proc.terminate()
        proc.wait(timeout=5)


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap_smoke(engine):
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    storage_config = json.dumps({
        "bucket": _BUCKET,
        "region": "us-east-1",
        "endpoint_url": "http://localhost:9000",
        "key_prefix": "smoke/",
    }).encode("utf-8")

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(Tenant(id=1, slug="d", display_name="D"))
        await s.flush()
        s.add(Project(id=1, tenant_id=1, name="d"))
        s.add(User(id=1, tenant_id=1, oidc_subject="d", email="d@l", role="tenant_admin"))
        s.add(StorageBackend(
            id=1, tenant_id=1, name="d", backend_type="s3",
            config_encrypted=storage_config, region="us-east-1",
        ))
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _set_token(monkeypatch: pytest.MonkeyPatch):
    get_settings.cache_clear()
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", SECRET)
    # ASGI transport has no real TLS — accept the client cert via the
    # trusted-proxy X-Client-Cert-PEM header bypass (same as test_executor_e2e).
    monkeypatch.setenv("DLW_TLS_TRUSTED_PROXY", "1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "minioadmin")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "minioadmin")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.manual
async def test_real_hf_to_minio_succeeds(minio_proc, ephemeral_ca) -> None:
    """Pull a real ~90MB public model from HF into local minio. ~30-90s."""
    app = make_app_with_state(ephemeral_ca, enrollment_token=_ENROLL)
    asgi_transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=asgi_transport, base_url="http://test"
    ) as ctrl_client:
        auth = principal_headers(secret=SECRET, role="tenant_admin")

        r = await ctrl_client.post("/api/v1/tasks", json={
            "repo_id": _REPO, "revision": _REVISION, "storage_id": 1,
        }, headers=auth)
        assert r.status_code == 201, r.text
        task_id = r.json()["id"]

        # W3a: register the executor via mTLS enrollment, then build the
        # AuthState the ControllerClient + runner drive off (post-SP1 the
        # single bearer_token client auth is gone — mTLS + executor JWT).
        reg = await register_test_executor(
            ctrl_client, enrollment_token=_ENROLL,
            executor_id="smoke-host-worker-1", host_id="smoke-host",
        )
        _far = _dt.datetime.now(_dt.UTC) + _dt.timedelta(hours=24)
        auth_state = AuthState(
            executor_id=reg["executor_id"], epoch=reg["epoch"],
            cert_pem=reg["cert_pem"].encode("utf-8"),
            key_pem=b"unused-in-asgi-transport-mode",
            ca_chain_pem="\n".join(reg["ca_chain"]).encode("utf-8"),
            jwt=reg["jwt"], jwt_exp=_far, cert_exp=_far,
            hmac_seed=reg["hmac_seed"], cert_dir=Path("."),
        )
        executor_client = ControllerClient(
            base_url="http://test",
            auth_state=auth_state,
            _transport=asgi_transport,
            # ASGI transport has no real TLS — feed the client cert via the
            # trusted-proxy header bypass.
            _extra_test_headers={
                "X-Client-Cert-PEM": reg["cert_pem"].replace("\n", "\\n"),
            },
        )
        settings = ExecutorSettings(
            id="smoke-host-worker-1", host_id="smoke-host",
            controller_url="http://test", bearer_token=_TOKEN,
            heartbeat_interval_seconds=2, poll_interval_seconds=2,
            s3_endpoint_url="http://localhost:9000",
        )
        downloader = HfS3StreamDownloader(
            settings=settings, client=executor_client,
        )
        runner = ExecutorRunner(
            settings=settings, client=executor_client,
            stream_downloader=downloader, chunk_downloader=MagicMock(),
            auth_state=auth_state,   # skip load_or_register
        )

        async with executor_client:
            run_task = asyncio.create_task(runner.run())
            # Wait for completion. Real HF (~90MB) proxied through the
            # controller + minio upload — generous budget so network
            # variance doesn't false-fail this manual smoke.
            for _ in range(120):  # 120 * 2s = 4min max
                await asyncio.sleep(2)
                tr = await ctrl_client.get(f"/api/v1/tasks/{task_id}", headers=auth)
                if tr.json()["status"] in ("succeeded", "failed"):
                    break
            runner.request_shutdown()
            await asyncio.wait_for(run_task, timeout=30)

        tr = await ctrl_client.get(f"/api/v1/tasks/{task_id}", headers=auth)
        body = tr.json()
        assert body["status"] == "succeeded", f"task did not succeed: {body}"

        # Spot-check S3 has objects
        keys = [o["Key"] for o in
                minio_proc.list_objects_v2(Bucket=_BUCKET).get("Contents", [])]
        assert len(keys) > 0
        assert any(k.endswith("config.json") for k in keys)
