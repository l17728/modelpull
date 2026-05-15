"""Tests for ExecutorSettings."""
from __future__ import annotations

import pytest

from dlw.executor.config import ExecutorSettings


@pytest.mark.slow
def test_defaults_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DLW_EXECUTOR_ID", "host-test-w1")
    monkeypatch.setenv("DLW_EXECUTOR_BEARER_TOKEN", "secret")
    s = ExecutorSettings()
    assert s.id == "host-test-w1"
    assert s.bearer_token == "secret"
    assert s.controller_url == "http://localhost:8000"
    assert s.heartbeat_interval_seconds == 10
    assert s.poll_interval_seconds == 2
    assert s.download_dir == "./downloads"


@pytest.mark.slow
def test_required_fields_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DLW_EXECUTOR_ID", raising=False)
    monkeypatch.delenv("DLW_EXECUTOR_BEARER_TOKEN", raising=False)
    with pytest.raises(Exception):
        ExecutorSettings()


@pytest.mark.slow
def test_host_id_defaults_to_id_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """If host_id not set, derive from id by stripping -worker-N suffix."""
    monkeypatch.setenv("DLW_EXECUTOR_ID", "host-12.local-worker-3")
    monkeypatch.setenv("DLW_EXECUTOR_BEARER_TOKEN", "x")
    monkeypatch.delenv("DLW_EXECUTOR_HOST_ID", raising=False)
    s = ExecutorSettings()
    assert s.host_id == "host-12.local"


@pytest.mark.slow
def test_w4_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 1 W4 S3 fields have safe defaults (AWS S3). HF fields removed in W3b."""
    monkeypatch.setenv("DLW_EXECUTOR_ID", "host-w4-worker-1")
    monkeypatch.setenv("DLW_EXECUTOR_BEARER_TOKEN", "secret")
    s = ExecutorSettings()
    assert s.s3_region == "us-east-1"
    assert s.s3_endpoint_url is None
    assert s.s3_path_style is True
    assert s.multipart_part_size_bytes == 5 * 1024 * 1024
    assert s.download_timeout_seconds == 300


@pytest.mark.slow
def test_w4_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DLW_EXECUTOR_ID", "host-w4-worker-2")
    monkeypatch.setenv("DLW_EXECUTOR_BEARER_TOKEN", "secret")
    monkeypatch.setenv("DLW_EXECUTOR_S3_ENDPOINT_URL", "http://minio:9000")
    monkeypatch.setenv("DLW_EXECUTOR_S3_REGION", "cn-east-1")
    s = ExecutorSettings()
    assert s.s3_endpoint_url == "http://minio:9000"
    assert s.s3_region == "cn-east-1"
