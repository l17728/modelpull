"""Tests for MockDownloader."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from dlw.executor.downloader import DownloadResult, MockDownloader


@pytest.fixture
def tmp_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.mark.slow
async def test_download_writes_file_of_correct_size(tmp_dir: Path) -> None:
    d = MockDownloader(download_dir=tmp_dir)
    result = await d.download(
        task_id="task-1", filename="model.safetensors", file_size=8192,
    )
    assert isinstance(result, DownloadResult)
    assert result.bytes_written == 8192
    file_path = tmp_dir / "task-1" / "model.safetensors"
    assert file_path.exists()
    assert file_path.stat().st_size == 8192


@pytest.mark.slow
async def test_download_returns_correct_sha256(tmp_dir: Path) -> None:
    d = MockDownloader(download_dir=tmp_dir, seed=42)
    result = await d.download(
        task_id="task-2", filename="config.json", file_size=4096,
    )
    file_path = tmp_dir / "task-2" / "config.json"
    expected = hashlib.sha256(file_path.read_bytes()).hexdigest()
    assert result.actual_sha256 == expected
    assert len(result.actual_sha256) == 64


@pytest.mark.slow
async def test_download_zero_bytes_succeeds(tmp_dir: Path) -> None:
    """file_size=0 (e.g., empty config) shouldn't crash."""
    d = MockDownloader(download_dir=tmp_dir)
    result = await d.download(
        task_id="task-3", filename="empty.json", file_size=0,
    )
    assert result.bytes_written == 0
    assert result.actual_sha256 == hashlib.sha256(b"").hexdigest()


@pytest.mark.slow
async def test_download_creates_subdirs(tmp_dir: Path) -> None:
    """Filenames with subpaths (e.g., 'subdir/model.bin') should auto-mkdir."""
    d = MockDownloader(download_dir=tmp_dir)
    await d.download(
        task_id="task-4", filename="weights/layer1.bin", file_size=128,
    )
    assert (tmp_dir / "task-4" / "weights" / "layer1.bin").exists()
