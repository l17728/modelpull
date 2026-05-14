"""W3b: Settings.hf_proxy_timeout_seconds config field."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from dlw.config import Settings


def test_settings_has_hf_proxy_timeout_default() -> None:
    s = Settings()
    assert s.hf_proxy_timeout_seconds == 300


def test_settings_hf_proxy_timeout_rejects_below_min() -> None:
    with pytest.raises(ValidationError):
        Settings(hf_proxy_timeout_seconds=5)


def test_settings_hf_proxy_timeout_rejects_above_max() -> None:
    with pytest.raises(ValidationError):
        Settings(hf_proxy_timeout_seconds=4000)
