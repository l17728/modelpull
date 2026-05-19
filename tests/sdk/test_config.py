"""server/token/config precedence resolution (SP4)."""
from __future__ import annotations

import pytest

from dlw.sdk._config import resolve
from dlw.sdk.errors import UsageError


def test_flag_beats_env(monkeypatch):
    monkeypatch.setenv("DLW_SERVER", "http://env:8000")
    monkeypatch.setenv("DLW_TOKEN", "envtok")
    r = resolve(server="http://flag:9000", token="flagtok", config_path="")
    assert r.server == "http://flag:9000"
    assert r.token == "flagtok"


def test_env_token_fallback_order(monkeypatch):
    monkeypatch.delenv("DLW_TOKEN", raising=False)
    monkeypatch.delenv("DLW_SERVER", raising=False)
    monkeypatch.setenv("DLW_SYSTEM_ADMIN_TOKEN", "systok")
    r = resolve(server=None, token=None, config_path="")
    assert r.token == "systok"
    assert r.server == "http://localhost:8000"


def test_config_file_used(tmp_path, monkeypatch):
    for v in ("DLW_SERVER", "DLW_TOKEN", "DLW_SYSTEM_ADMIN_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "current_context: dev\n"
        "contexts:\n  dev:\n    server: http://cfg:7000\n"
        "auth:\n  dev:\n    access_token: cfgtok\n")
    r = resolve(server=None, token=None, config_path=str(cfg))
    assert r.server == "http://cfg:7000"
    assert r.token == "cfgtok"


def test_missing_token_raises_usage(tmp_path, monkeypatch):
    for v in ("DLW_TOKEN", "DLW_SYSTEM_ADMIN_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    with pytest.raises(UsageError):
        resolve(server="http://x", token=None,
                config_path=str(tmp_path / "none.yaml"))


def test_server_trailing_slash_stripped(monkeypatch):
    monkeypatch.setenv("DLW_TOKEN", "t")
    r = resolve(server="http://x:8000/", token=None, config_path="")
    assert r.server == "http://x:8000"
