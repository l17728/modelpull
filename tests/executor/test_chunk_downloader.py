"""Tests for DirectOffsetDownloader (Phase 2 W2b1)."""
from __future__ import annotations

import pytest

from dlw.executor.chunk_downloader import ChunkPlan, DiskFullError, plan_chunks


def test_plan_chunks_splits_evenly() -> None:
    plans = plan_chunks(100, 30)
    assert plans == [
        ChunkPlan(index=0, offset=0,  length=30),
        ChunkPlan(index=1, offset=30, length=30),
        ChunkPlan(index=2, offset=60, length=30),
        ChunkPlan(index=3, offset=90, length=10),
    ]


def test_plan_chunks_exact_multiple() -> None:
    plans = plan_chunks(60, 30)
    assert plans == [
        ChunkPlan(index=0, offset=0,  length=30),
        ChunkPlan(index=1, offset=30, length=30),
    ]


def test_plan_chunks_smaller_than_chunk_size() -> None:
    plans = plan_chunks(5 * 1024 * 1024, 16 * 1024 * 1024)
    assert plans == [ChunkPlan(index=0, offset=0, length=5 * 1024 * 1024)]


def test_plan_chunks_zero_file_size_returns_empty() -> None:
    assert plan_chunks(0, 16 * 1024 * 1024) == []


def test_disk_full_error_is_exception_subclass() -> None:
    """Smoke: ensure the public DiskFullError is importable and an Exception."""
    e = DiskFullError("ENOSPC writing chunk 3")
    assert isinstance(e, Exception)
    assert "ENOSPC" in str(e)
