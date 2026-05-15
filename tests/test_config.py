"""Tests for the controller Settings class (dlw.config)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from dlw.config import Settings


def test_settings_has_hf_proxy_timeout_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DLW_HF_PROXY_TIMEOUT_SECONDS", raising=False)
    s = Settings()
    assert s.hf_proxy_timeout_seconds == 300


def test_settings_hf_proxy_timeout_rejects_below_min() -> None:
    with pytest.raises(ValidationError):
        Settings(hf_proxy_timeout_seconds=5)


def test_settings_hf_proxy_timeout_rejects_above_max() -> None:
    with pytest.raises(ValidationError):
        Settings(hf_proxy_timeout_seconds=4000)


def test_settings_hf_proxy_timeout_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DLW_HF_PROXY_TIMEOUT_SECONDS", "600")
    s = Settings()
    assert s.hf_proxy_timeout_seconds == 600
