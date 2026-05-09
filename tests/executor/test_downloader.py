"""Tests for HfS3StreamDownloader — skeleton + helpers (W4 Task 9).

Pipeline tests (HF→S3 stream) come in W4 Task 10/11.
"""
from __future__ import annotations

import pytest

from dlw.executor.config import ExecutorSettings
from dlw.executor.downloader import (
    Assignment,
    DownloadResult,
    HfS3StreamDownloader,
    StorageConfig,
)


def _settings() -> ExecutorSettings:
    return ExecutorSettings(
        id="host-test-worker-1",
        bearer_token="t",
    )


def _assignment(*, repo_id="o/r", revision="a" * 40, filename="config.json",
                key_prefix="phase1/", bucket="b") -> Assignment:
    import uuid as _uuid
    return Assignment(
        subtask_id=_uuid.uuid4(),
        task_id=_uuid.uuid4(),
        repo_id=repo_id, revision=revision, filename=filename,
        file_size=4096, expected_sha256=None,
        storage_config=StorageConfig(bucket=bucket, key_prefix=key_prefix),
    )


def test_compose_key_includes_prefix_repo_revision_filename() -> None:
    d = HfS3StreamDownloader(settings=_settings())
    a = _assignment(filename="model.safetensors", key_prefix="phase1/")
    key = d._compose_key(a)
    assert key == "phase1/o/r/" + ("a" * 40) + "/model.safetensors"


def test_compose_key_handles_empty_prefix() -> None:
    d = HfS3StreamDownloader(settings=_settings())
    a = _assignment(key_prefix="")
    key = d._compose_key(a)
    assert key.startswith("o/r/")


def test_compose_key_strips_prefix_trailing_slash() -> None:
    d = HfS3StreamDownloader(settings=_settings())
    a = _assignment(key_prefix="phase1////")
    key = d._compose_key(a)
    # No double slashes, single separator
    assert "//" not in key
    assert key.startswith("phase1/o/r/")
