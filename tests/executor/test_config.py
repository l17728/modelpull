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
