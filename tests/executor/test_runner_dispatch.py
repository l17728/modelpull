"""Tests for ExecutorRunner._choose_downloader (Phase 2 W2b1 M3)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from dlw.executor.config import ExecutorSettings
from dlw.executor.runner import ExecutorRunner


def _runner_with_mocks(file_size_threshold: int):
    settings = ExecutorSettings(
        id="ex-dispatch",
        bearer_token="t",
        chunk_level_threshold_bytes=file_size_threshold,
    )
    client = MagicMock()
    stream = MagicMock()
    stream.download = AsyncMock()
    chunk = MagicMock()
    chunk.download = AsyncMock()
    runner = ExecutorRunner(
        settings=settings, client=client,
        stream_downloader=stream, chunk_downloader=chunk,
    )
    return runner, stream, chunk


def test_runner_picks_stream_for_small_file() -> None:
    runner, stream, chunk = _runner_with_mocks(file_size_threshold=100 * 1024 * 1024)
    chosen = runner._choose_downloader(file_size=50 * 1024 * 1024)
    assert chosen is stream


def test_runner_picks_chunk_for_large_file_and_for_unknown() -> None:
    runner, stream, chunk = _runner_with_mocks(file_size_threshold=100 * 1024 * 1024)
    assert runner._choose_downloader(file_size=200 * 1024 * 1024) is chunk
    assert runner._choose_downloader(file_size=None) is chunk
    # Boundary: exactly threshold → chunk (uses >= comparison).
    assert runner._choose_downloader(file_size=100 * 1024 * 1024) is chunk
