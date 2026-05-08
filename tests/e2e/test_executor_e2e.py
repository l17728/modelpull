"""E2E: real controller in-process + real ExecutorRunner — full happy path.

W3-C/D: monkeypatches _MOCK_FILES to small sizes so the test actually finishes
in seconds. A 1GB random-bytes generation would block the event loop for many
seconds even with asyncio.to_thread (the thread is just doing CPU work).

W3-E: shares one httpx.ASGITransport(app=app) instance between the controller's
test AsyncClient and the executor's ControllerClient — no private attribute
access needed.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.config import get_settings
from dlw.db.base import Base
from dlw.executor.client import ControllerClient
from dlw.executor.config import ExecutorSettings
from dlw.executor.downloader import MockDownloader
from dlw.executor.runner import ExecutorRunner


_TOKEN = "e2e-executor-token"


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    """Seed default tenant/project/user/storage. engine is session-scoped (conftest)."""
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Project, Tenant, User
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
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
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def _set_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DLW_BEARER_TOKEN", _TOKEN)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.slow
async def test_executor_completes_real_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end with real controller + real executor (no mocks)."""
    # W3-D: shrink mock files so 1GB safetensors doesn't burn 30+ seconds of CPU
    import dlw.services.task_service as ts
    monkeypatch.setattr(ts, "_MOCK_FILES", [
        ("config.json", 4096, None),
        ("model.safetensors", 64 * 1024, None),
    ])

    from dlw.main import create_app
    app = create_app()

    # W3-E: single shared ASGITransport, no private attribute access
    asgi_transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=asgi_transport, base_url="http://test"
    ) as ctrl_client:
        auth = {"Authorization": f"Bearer {_TOKEN}"}

        r = await ctrl_client.post("/api/v1/tasks", json={
            "repo_id": "o/e2e",
            "revision": "0" * 40,
            "storage_id": 1,
        }, headers=auth)
        assert r.status_code == 201
        task_id = r.json()["id"]

        executor_client = ControllerClient(
            base_url="http://test",
            bearer_token=_TOKEN,
            _transport=asgi_transport,
        )

        settings = ExecutorSettings(
            id="e2e-host-w1",
            host_id="e2e-host",
            controller_url="http://test",
            bearer_token=_TOKEN,
            heartbeat_interval_seconds=1,
            poll_interval_seconds=1,
            download_dir=str(tmp_path),
        )
        downloader = MockDownloader(download_dir=tmp_path)
        runner = ExecutorRunner(
            settings=settings, client=executor_client, downloader=downloader
        )

        async with executor_client:
            run_task = asyncio.create_task(runner.run())
            await asyncio.sleep(4)
            runner.request_shutdown()
            await asyncio.wait_for(run_task, timeout=5)

        r = await ctrl_client.get(f"/api/v1/tasks/{task_id}", headers=auth)
        assert r.json()["status"] == "succeeded", r.json()
        assert r.json()["completed_at"] is not None

        files = list(tmp_path.rglob("*"))
        file_names = {p.name for p in files if p.is_file()}
        assert "config.json" in file_names
        assert "model.safetensors" in file_names
