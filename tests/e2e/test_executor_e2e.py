"""E2E: real controller + real ExecutorRunner — full HF→S3 happy path.

W4 rewrite: replaces MockDownloader with HfS3StreamDownloader; HF served by
httpx MockTransport (returns deterministic bytes per filename); S3 served by
moto[s3] in-process. No Docker required.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import httpx
import pytest
from moto import mock_aws
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.config import get_settings
from dlw.db.base import Base
from dlw.executor.client import ControllerClient
from dlw.executor.config import ExecutorSettings
from dlw.executor.downloader import HfS3StreamDownloader
from dlw.executor.runner import ExecutorRunner
from dlw.schemas.storage import StorageConfig
from dlw.services.hf_metadata import RepoFile


_TOKEN = "e2e-w4-token"
_BUCKET = "e2e-bucket"


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    """Tenant + project + user + storage row with proper JSON config."""
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    storage_config = json.dumps({
        "bucket": _BUCKET,
        "region": "us-east-1",
        "endpoint_url": None,        # moto via env
        "key_prefix": "phase1/",
    }).encode("utf-8")

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(Tenant(id=1, slug="d", display_name="D"))
        await s.flush()
        s.add(Project(id=1, tenant_id=1, name="d"))
        s.add(User(id=1, tenant_id=1, oidc_subject="d",
                   email="d@l", role="tenant_admin"))
        s.add(StorageBackend(
            id=1, tenant_id=1, name="d", backend_type="s3",
            config_encrypted=storage_config, region="us-east-1",
        ))
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


_ENROLL = "e2e-w4-enrollment-token"


@pytest.fixture(autouse=True)
def _set_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DLW_BEARER_TOKEN", _TOKEN)
    monkeypatch.setenv("DLW_TLS_TRUSTED_PROXY", "1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.slow
async def test_e2e_hf_to_s3_full_pipeline(
    monkeypatch: pytest.MonkeyPatch, ephemeral_ca,
) -> None:
    """End-to-end with mocked HF + moto S3, no MockDownloader."""
    # Mock HF metadata (controller side)
    file_a_bytes = b"a" * 4096
    file_b_bytes = b"b" * (64 * 1024)
    sha_a = hashlib.sha256(file_a_bytes).hexdigest()
    sha_b = hashlib.sha256(file_b_bytes).hexdigest()

    async def fake_list_repo_tree(*args, **kwargs):
        # Note: sha is the LFS sha — config.json is non-LFS in real life,
        # but for the test we set it so we can also exercise the verify path.
        return [
            RepoFile(path="config.json", size=len(file_a_bytes), sha256=sha_a),
            RepoFile(path="model.safetensors", size=len(file_b_bytes), sha256=sha_b),
        ]
    monkeypatch.setattr(
        "dlw.services.task_service.list_repo_tree", fake_list_repo_tree,
    )

    # Mock HF download (executor side)
    def hf_handler(request: httpx.Request) -> httpx.Response:
        # URL shape: {endpoint}/{repo}/resolve/{revision}/{filename}
        path = request.url.path
        if path.endswith("/config.json"):
            return httpx.Response(200, content=file_a_bytes)
        if path.endswith("/model.safetensors"):
            return httpx.Response(200, content=file_b_bytes)
        return httpx.Response(404, content=b"unexpected url")
    hf_transport = httpx.MockTransport(hf_handler)

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=_BUCKET)

        from dlw.main import create_app
        from dlw.auth.hmac_nonce import NonceStore
        from dlw.executor.auth_lifecycle import AuthState
        from tests.conftest import register_test_executor
        app = create_app()
        # W3a: inject the auth substrate onto app.state (skip the lifespan
        # bootstrap — this test drives the ASGI app directly, no real server).
        app.state.ca = ephemeral_ca["ca"]
        app.state.jwt_keypair = ephemeral_ca["jwt_keypair"]
        app.state.nonce_store = NonceStore(maxsize=1000, ttl_seconds=300)
        app.state.enrollment_token = _ENROLL
        asgi_transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(
            transport=asgi_transport, base_url="http://test"
        ) as ctrl_client:
            auth = {"Authorization": f"Bearer {_TOKEN}"}

            r = await ctrl_client.post("/api/v1/tasks", json={
                "repo_id": "o/e2e-w4",
                "revision": "0" * 40,
                "storage_id": 1,
            }, headers=auth)
            assert r.status_code == 201, r.text
            task_id = r.json()["id"]

            # W3a: register the executor via mTLS enrollment, then build an
            # AuthState the ControllerClient + runner drive off.
            reg = await register_test_executor(
                ctrl_client, enrollment_token=_ENROLL,
                executor_id="e2e-w4-host-worker-1", host_id="e2e-w4-host",
            )
            import datetime as _dt
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
                # ASGI transport has no real TLS — feed the cert via the
                # trusted-proxy header bypass (DLW_TLS_TRUSTED_PROXY=1).
                _extra_test_headers={
                    "X-Client-Cert-PEM": reg["cert_pem"].replace("\n", "\\n"),
                },
            )
            settings = ExecutorSettings(
                id="e2e-w4-host-worker-1",
                host_id="e2e-w4-host",
                controller_url="http://test",
                bearer_token=_TOKEN,
                heartbeat_interval_seconds=1,
                poll_interval_seconds=1,
            )
            downloader = HfS3StreamDownloader(
                settings=settings, client=executor_client,
            )
            # W3b: the executor fetches HF bytes through the controller's
            # reverse proxy. Install the HF MockTransport on the controller's
            # HF client factory (not the downloader).
            import dlw.api.hf_proxy as _hf_proxy_mod
            monkeypatch.setattr(
                _hf_proxy_mod, "_make_hf_client",
                lambda timeout_seconds: httpx.AsyncClient(
                    transport=hf_transport, follow_redirects=True,
                ),
            )

            runner = ExecutorRunner(
                settings=settings, client=executor_client,
                stream_downloader=downloader, chunk_downloader=MagicMock(),
                auth_state=auth_state,   # skip load_or_register
            )

            async with executor_client:
                run_task = asyncio.create_task(runner.run())
                # Allow time: 2 polls + 2 downloads
                await asyncio.sleep(5)
                runner.request_shutdown()
                await asyncio.wait_for(run_task, timeout=10)

            r = await ctrl_client.get(f"/api/v1/tasks/{task_id}", headers=auth)
            body = r.json()
            assert body["status"] == "succeeded", body
            assert body["completed_at"] is not None

            # Verify both files in S3
            keys = [o["Key"] for o in
                    s3.list_objects_v2(Bucket=_BUCKET).get("Contents", [])]
            assert any(k.endswith("/config.json") for k in keys)
            assert any(k.endswith("/model.safetensors") for k in keys)
