"""Tests for runner classifying HF 429/503 as paused_external (Phase 2 W2b2 §3.6)."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from dlw.executor.config import ExecutorSettings
from dlw.executor.runner import ExecutorRunner


def _make_http_status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "http://hf.fake/repo/file")
    resp = httpx.Response(status_code=code, request=req)
    return httpx.HTTPStatusError(f"HTTP {code}", request=req, response=resp)


def _runner_with_failing_downloader(error: Exception):
    settings = ExecutorSettings(id="ex-throttle", bearer_token="t")
    client = MagicMock()
    client.report = AsyncMock()
    stream = MagicMock()
    stream.download = AsyncMock(side_effect=error)
    chunk = MagicMock()
    chunk.download = AsyncMock(side_effect=error)
    return ExecutorRunner(
        settings=settings, client=client,
        stream_downloader=stream, chunk_downloader=chunk,
    ), client


def _build_subtask_dict(sub_id: uuid.UUID) -> dict:
    return {
        "id": str(sub_id),
        "task_id": str(uuid.uuid4()),
        "filename": "m.bin",
        "file_size": 50_000_000,    # below 100 MiB threshold → stream_downloader
        "expected_sha256": None,
    }


_STORAGE_CFG = {
    "bucket": "test", "region": "us-east-1",
    "endpoint_url": None,
    "access_key_id": "x", "secret_access_key": "y",
    "key_prefix": "",
}


async def test_runner_classifies_429_as_paused_external() -> None:
    err = _make_http_status_error(429)
    runner, client = _runner_with_failing_downloader(err)
    sub_id = uuid.uuid4()
    token = uuid.uuid4()

    await runner._execute_subtask(
        subtask=_build_subtask_dict(sub_id),
        assignment_token=token,
        repo_id="o/r",
        revision="b" * 40,
        storage_config=_STORAGE_CFG,
    )

    client.report.assert_awaited_once()
    kwargs = client.report.await_args.kwargs
    assert kwargs["status"] == "paused_external"
    assert kwargs["error"] == "HTTP 429"


async def test_runner_classifies_503_as_paused_external() -> None:
    err = _make_http_status_error(503)
    runner, client = _runner_with_failing_downloader(err)
    sub_id = uuid.uuid4()
    token = uuid.uuid4()

    await runner._execute_subtask(
        subtask=_build_subtask_dict(sub_id),
        assignment_token=token,
        repo_id="o/r",
        revision="b" * 40,
        storage_config=_STORAGE_CFG,
    )

    client.report.assert_awaited_once()
    kwargs = client.report.await_args.kwargs
    assert kwargs["status"] == "paused_external"
    assert kwargs["error"] == "HTTP 503"
