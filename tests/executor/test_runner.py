"""Tests for ExecutorRunner main loop."""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from dlw.executor.client import ControllerClient
from dlw.executor.config import ExecutorSettings
from dlw.executor.downloader import DownloadResult, MockDownloader
from dlw.executor.runner import ExecutorRunner


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ExecutorSettings:
    monkeypatch.setenv("DLW_EXECUTOR_ID", "host-test-w1")
    monkeypatch.setenv("DLW_EXECUTOR_BEARER_TOKEN", "secret")
    monkeypatch.setenv("DLW_EXECUTOR_DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setenv("DLW_EXECUTOR_HEARTBEAT_INTERVAL_SECONDS", "1")
    monkeypatch.setenv("DLW_EXECUTOR_POLL_INTERVAL_SECONDS", "1")
    return ExecutorSettings()


@pytest.mark.slow
async def test_runner_join_then_heartbeat_in_idle(settings) -> None:
    """When poll always returns assigned=False, runner heartbeats but does not download."""
    client = MagicMock(spec=ControllerClient)
    client.join = AsyncMock(return_value={"id": "host-test-w1", "status": "joining", "health_score": 100})
    client.heartbeat = AsyncMock(return_value={"id": "x", "status": "healthy", "health_score": 100})
    client.poll = AsyncMock(return_value={"assigned": False, "subtask": None, "assignment_token": None})
    downloader = MagicMock(spec=MockDownloader)

    runner = ExecutorRunner(settings=settings, client=client, downloader=downloader)
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(2.5)
    runner.request_shutdown()
    await asyncio.wait_for(task, timeout=5)

    client.join.assert_awaited_once()
    assert client.heartbeat.await_count >= 1
    assert client.poll.await_count >= 1
    downloader.download.assert_not_called()


@pytest.mark.slow
async def test_runner_executes_assigned_subtask(settings) -> None:
    """When poll returns an assignment, runner downloads + reports."""
    sub_id = uuid.uuid4()
    token = uuid.uuid4()

    client = MagicMock(spec=ControllerClient)
    client.join = AsyncMock(return_value={"id": "host-test-w1", "status": "joining", "health_score": 100})
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
        },
        {"assigned": False, "subtask": None, "assignment_token": None},
    ]
    client.poll = AsyncMock(side_effect=lambda **kw: poll_results.pop(0) if poll_results else {"assigned": False, "subtask": None, "assignment_token": None})

    download_result = DownloadResult(
        bytes_written=1024, actual_sha256="a" * 64,
        file_path=Path(settings.download_dir) / "task-1" / "config.json",
    )
    downloader = MagicMock(spec=MockDownloader)
    downloader.download = AsyncMock(return_value=download_result)

    client.report = AsyncMock(return_value={"subtask_status": "succeeded", "task_status": "pending"})

    runner = ExecutorRunner(settings=settings, client=client, downloader=downloader)
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
async def test_runner_reports_failure_on_download_error(settings) -> None:
    """If downloader raises, runner reports status=failed with the error message."""
    sub_id = uuid.uuid4()
    token = uuid.uuid4()

    client = MagicMock(spec=ControllerClient)
    client.join = AsyncMock(return_value={"id": "host-test-w1", "status": "joining", "health_score": 100})
    client.heartbeat = AsyncMock(return_value={"id": "x", "status": "healthy", "health_score": 100})
    client.poll = AsyncMock(side_effect=[
        {
            "assigned": True,
            "subtask": {"id": str(sub_id), "task_id": "x", "filename": "f", "file_size": 100, "expected_sha256": None, "status": "assigned"},
            "assignment_token": str(token),
        },
        {"assigned": False, "subtask": None, "assignment_token": None},
    ])
    downloader = MagicMock(spec=MockDownloader)
    downloader.download = AsyncMock(side_effect=OSError("disk full"))
    client.report = AsyncMock(return_value={"subtask_status": "failed", "task_status": "failed"})

    runner = ExecutorRunner(settings=settings, client=client, downloader=downloader)
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(2.5)
    runner.request_shutdown()
    await asyncio.wait_for(task, timeout=5)

    client.report.assert_awaited_once()
    call = client.report.await_args
    assert call.kwargs["status"] == "failed"
    assert "disk full" in call.kwargs["error"]


@pytest.mark.slow
async def test_runner_graceful_shutdown(settings) -> None:
    """request_shutdown() during execution should cleanly cancel the loops."""
    client = MagicMock(spec=ControllerClient)
    client.join = AsyncMock(return_value={"id": "x", "status": "joining", "health_score": 100})
    client.heartbeat = AsyncMock(return_value={"id": "x", "status": "healthy", "health_score": 100})
    client.poll = AsyncMock(return_value={"assigned": False, "subtask": None, "assignment_token": None})
    downloader = MagicMock(spec=MockDownloader)

    runner = ExecutorRunner(settings=settings, client=client, downloader=downloader)
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(0.3)
    runner.request_shutdown()
    await asyncio.wait_for(task, timeout=3)
