"""Tests for ExecutorRunner main loop (W3a: mTLS+JWT auth bootstrap)."""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from dlw.executor.client import ControllerClient
from dlw.executor.config import ExecutorSettings
from dlw.executor.downloader import DownloadResult, HfS3StreamDownloader
from dlw.executor.runner import ExecutorRunner
from tests.conftest import make_fake_auth_state


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ExecutorSettings:
    monkeypatch.setenv("DLW_EXECUTOR_ID", "host-test-w1")
    monkeypatch.setenv("DLW_EXECUTOR_BEARER_TOKEN", "secret")
    monkeypatch.setenv("DLW_EXECUTOR_DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setenv("DLW_EXECUTOR_HEARTBEAT_INTERVAL_SECONDS", "1")
    monkeypatch.setenv("DLW_EXECUTOR_POLL_INTERVAL_SECONDS", "1")
    return ExecutorSettings()


@pytest.fixture
def auth_state(tmp_path):
    """A pre-built AuthState injected into the runner so run() skips
    load_or_register (no HTTP call to a controller in these unit tests)."""
    return make_fake_auth_state(tmp_path, executor_id="host-test-w1", epoch=1)


@pytest.mark.slow
async def test_runner_heartbeats_in_idle(settings, auth_state) -> None:
    """When poll always returns assigned=False, runner heartbeats but does not download."""
    client = MagicMock(spec=ControllerClient)
    client.heartbeat = AsyncMock(return_value={"id": "x", "status": "healthy", "health_score": 100})
    client.poll = AsyncMock(return_value={"assigned": False, "subtask": None, "assignment_token": None})
    downloader = MagicMock(spec=HfS3StreamDownloader)

    runner = ExecutorRunner(
        settings=settings, client=client,
        stream_downloader=downloader, chunk_downloader=MagicMock(),
        auth_state=auth_state,
    )
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(2.5)
    runner.request_shutdown()
    await asyncio.wait_for(task, timeout=5)

    client.update_auth.assert_called_once()
    assert client.heartbeat.await_count >= 1
    assert client.poll.await_count >= 1
    downloader.download.assert_not_called()


@pytest.mark.slow
async def test_runner_executes_assigned_subtask(settings, auth_state) -> None:
    """When poll returns an assignment, runner downloads + reports."""
    sub_id = uuid.uuid4()
    token = uuid.uuid4()

    client = MagicMock(spec=ControllerClient)
    client.heartbeat = AsyncMock(return_value={"id": "x", "status": "healthy", "health_score": 100})

    poll_results = [
        {
            "assigned": True,
            "subtask": {
                "id": str(sub_id),
                "task_id": str(uuid.uuid4()),
                "filename": "config.json",
                "file_size": 1024,
                "expected_sha256": None,
                "status": "assigned",
            },
            "assignment_token": str(token),
            "repo_id": "o/test-repo",
            "revision": "a" * 40,
            "storage_config": {
                "bucket": "test-bucket", "region": "us-east-1",
                "endpoint_url": None, "key_prefix": "prefix/",
            },
        },
        {"assigned": False, "subtask": None, "assignment_token": None},
    ]
    client.poll = AsyncMock(side_effect=lambda **kw: poll_results.pop(0) if poll_results else {"assigned": False, "subtask": None, "assignment_token": None})

    download_result = DownloadResult(
        bytes_written=1024, actual_sha256="a" * 64,
        s3_key="prefix/o/test-repo/" + "a" * 40 + "/config.json",
    )
    downloader = MagicMock(spec=HfS3StreamDownloader)
    downloader.download = AsyncMock(return_value=download_result)
    client.report = AsyncMock(return_value={"subtask_status": "succeeded", "task_status": "pending"})

    runner = ExecutorRunner(
        settings=settings, client=client,
        stream_downloader=downloader, chunk_downloader=MagicMock(),
        auth_state=auth_state,
    )
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(2.5)
    runner.request_shutdown()
    await asyncio.wait_for(task, timeout=5)

    downloader.download.assert_awaited_once()
    client.report.assert_awaited_once()
    call = client.report.await_args
    assert call.kwargs["status"] == "succeeded"
    assert call.kwargs["assignment_token"] == token
    assert call.kwargs["bytes_downloaded"] == 1024


@pytest.mark.slow
async def test_runner_reports_failure_on_download_error(settings, auth_state) -> None:
    """If downloader raises, runner reports status=failed with the error message."""
    sub_id = uuid.uuid4()
    token = uuid.uuid4()

    client = MagicMock(spec=ControllerClient)
    client.heartbeat = AsyncMock(return_value={"id": "x", "status": "healthy", "health_score": 100})
    client.poll = AsyncMock(side_effect=[
        {
            "assigned": True,
            "subtask": {"id": str(sub_id), "task_id": str(uuid.uuid4()), "filename": "f", "file_size": 100, "expected_sha256": None, "status": "assigned"},
            "assignment_token": str(token),
            "repo_id": "o/fail-repo",
            "revision": "b" * 40,
            "storage_config": {
                "bucket": "fail-bucket", "region": "us-east-1",
                "endpoint_url": None, "key_prefix": "fp/",
            },
        },
        {"assigned": False, "subtask": None, "assignment_token": None},
    ])
    downloader = MagicMock(spec=HfS3StreamDownloader)
    downloader.download = AsyncMock(side_effect=OSError("disk full"))
    client.report = AsyncMock(return_value={"subtask_status": "failed", "task_status": "failed"})

    runner = ExecutorRunner(
        settings=settings, client=client,
        stream_downloader=downloader, chunk_downloader=MagicMock(),
        auth_state=auth_state,
    )
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(2.5)
    runner.request_shutdown()
    await asyncio.wait_for(task, timeout=5)

    client.report.assert_awaited_once()
    call = client.report.await_args
    assert call.kwargs["status"] == "failed"
    assert "disk full" in call.kwargs["error"]


@pytest.mark.slow
async def test_runner_graceful_shutdown(settings, auth_state) -> None:
    """request_shutdown() during execution should cleanly cancel the loops."""
    client = MagicMock(spec=ControllerClient)
    client.heartbeat = AsyncMock(return_value={"id": "x", "status": "healthy", "health_score": 100})
    client.poll = AsyncMock(return_value={"assigned": False, "subtask": None, "assignment_token": None})
    downloader = MagicMock(spec=HfS3StreamDownloader)

    runner = ExecutorRunner(
        settings=settings, client=client,
        stream_downloader=downloader, chunk_downloader=MagicMock(),
        auth_state=auth_state,
    )
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(0.3)
    runner.request_shutdown()
    await asyncio.wait_for(task, timeout=3)


@pytest.mark.slow
async def test_runner_passes_assignment_with_repo_and_storage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """W4: runner forwards repo_id/revision/storage_config from /poll to downloader."""
    from dlw.executor.downloader import Assignment, DownloadResult

    captured: dict[str, object] = {}

    class FakeDownloader:
        async def download(self, *, assignment: Assignment) -> DownloadResult:
            captured["assignment"] = assignment
            prefix = assignment.storage_config.key_prefix.strip("/")
            return DownloadResult(
                bytes_written=42,
                actual_sha256="a" * 64,
                s3_key=f"{prefix}/{assignment.repo_id}/{assignment.revision}/{assignment.filename}",
            )

    class FakeClient:
        joined_polls = 0
        def update_auth(self, _state): pass
        async def heartbeat(self, **kw): pass
        async def poll(self, **kw):
            FakeClient.joined_polls += 1
            if FakeClient.joined_polls > 1:
                return {"assigned": False}
            import uuid as u
            return {
                "assigned": True,
                "subtask": {
                    "id": str(u.uuid4()), "task_id": str(u.uuid4()),
                    "filename": "config.json", "file_size": 4096,
                    "expected_sha256": None, "status": "assigned",
                },
                "assignment_token": str(u.uuid4()),
                "repo_id": "o/runner-test", "revision": "z" * 40,
                "storage_config": {
                    "bucket": "b", "region": "us-east-1",
                    "endpoint_url": None, "key_prefix": "p/",
                },
            }
        async def report(self, **kw): captured["report_kw"] = kw

    settings = ExecutorSettings(
        id="host-r-worker-1", bearer_token="t",
        heartbeat_interval_seconds=1, poll_interval_seconds=1,
    )
    runner = ExecutorRunner(
        settings=settings, client=FakeClient(),
        stream_downloader=FakeDownloader(), chunk_downloader=MagicMock(),
        auth_state=make_fake_auth_state(tmp_path, executor_id="host-r-worker-1"),
    )
    run_task = asyncio.create_task(runner.run())
    await asyncio.sleep(2)
    runner.request_shutdown()
    await asyncio.wait_for(run_task, timeout=5)

    a = captured["assignment"]
    assert a.repo_id == "o/runner-test"
    assert a.revision == "z" * 40
    assert a.storage_config.bucket == "b"
    assert a.storage_config.key_prefix == "p/"
    assert captured["report_kw"]["status"] == "succeeded"
    assert captured["report_kw"]["s3_key"].startswith("p/o/runner-test/")


@pytest.mark.slow
async def test_runner_reregisters_on_poll_401(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """W3a: on a 401 from poll, the runner re-registers (load_or_register) and
    continues. Generalizes the W1 EPOCH_MISMATCH re-join path."""
    import httpx as _httpx
    import dlw.executor.runner as runner_mod

    register_calls: list[int] = []

    async def fake_load_or_register(**kwargs):
        register_calls.append(1)
        return make_fake_auth_state(
            tmp_path, executor_id=kwargs["executor_id"],
            epoch=len(register_calls),
        )

    monkeypatch.setattr(runner_mod, "load_or_register", fake_load_or_register)

    class FakeClient:
        def __init__(self):
            self.calls: list[str] = []
            self._poll_401_once = True
        def update_auth(self, _state):
            self.calls.append("update_auth")
        async def heartbeat(self, **kw):
            self.calls.append("heartbeat")
        async def poll(self, **kw):
            self.calls.append("poll")
            if self._poll_401_once:
                self._poll_401_once = False
                req = _httpx.Request("POST", "http://x/poll")
                resp = _httpx.Response(401, json={"detail": "invalid JWT"})
                raise _httpx.HTTPStatusError("401", request=req, response=resp)
            return {"assigned": False}
        async def report(self, **kw): pass

    class FakeDl:
        async def download(self, **kw):
            raise AssertionError("downloader should NOT be invoked in this test")

    settings = ExecutorSettings(
        id="rj-host-worker-1", host_id="rj-host", bearer_token="t",
        heartbeat_interval_seconds=1, poll_interval_seconds=1,
    )
    runner = ExecutorRunner(
        settings=settings, client=FakeClient(),
        stream_downloader=FakeDl(), chunk_downloader=MagicMock(),
        # auth_state=None → run() calls (the monkeypatched) load_or_register.
    )
    run_task = asyncio.create_task(runner.run())
    await asyncio.sleep(2.5)
    runner.request_shutdown()
    await asyncio.wait_for(run_task, timeout=3)

    # load_or_register called at least twice: initial bootstrap + after the 401.
    assert len(register_calls) >= 2
    assert "poll" in runner._client.calls
