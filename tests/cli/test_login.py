"""Tests for `dlw login` / `dlw logout` CLI commands (FU6)."""
from __future__ import annotations

import pytest

from dlw.cli import main as cli
from dlw.sdk._config import load_config, set_context

# ---------------------------------------------------------------------------
# test_login_device_writes_token
# ---------------------------------------------------------------------------

def test_login_device_writes_token(monkeypatch, tmp_path):
    """Device flow: monkeypatch device_authorize + poll_for_token, assert token written."""
    p = str(tmp_path / "c.yaml")

    monkeypatch.setattr(
        "dlw.cli.handlers.device_authorize",
        lambda server, **kw: {
            "device_code": "dc",
            "user_code": "BCDF-GHJK",
            "verification_uri": "/device",
            "verification_uri_complete": "/device?user_code=BCDF-GHJK",
            "expires_in": 600,
            "interval": 0,
        },
    )
    monkeypatch.setattr(
        "dlw.cli.handlers.poll_for_token",
        lambda server, device_code, **kw: {
            "access_token": "TOK",
            "token_type": "Bearer",
            "expires_in": 3600,
            "tenant_id": 1,
            "role": "tenant_operator",
        },
    )

    rc = cli.main(["-c", p, "login", "--no-browser", "--context", "test",
                   "--server", "http://x"])
    assert rc == 0

    cfg = load_config(p)
    assert cfg["auth"]["test"]["access_token"] == "TOK"
    assert cfg["current_context"] == "test"


# ---------------------------------------------------------------------------
# test_login_token_shortcut
# ---------------------------------------------------------------------------

def test_login_token_shortcut(monkeypatch, tmp_path):
    """--token shortcut: device_authorize must NOT be called."""
    p = str(tmp_path / "c.yaml")

    def _should_not_be_called(*args, **kwargs):
        raise AssertionError("device_authorize should not be called in token shortcut path")

    monkeypatch.setattr("dlw.cli.handlers.device_authorize", _should_not_be_called)

    rc = cli.main(["-c", p, "login", "--token", "T", "--context", "test",
                   "--server", "http://x"])
    assert rc == 0

    cfg = load_config(p)
    assert cfg["auth"]["test"]["access_token"] == "T"


# ---------------------------------------------------------------------------
# test_logout_clears_token
# ---------------------------------------------------------------------------

def test_logout_clears_token(tmp_path):
    """logout: removes access_token from the named context."""
    p = str(tmp_path / "c.yaml")

    # Pre-write a context with a token.
    set_context("test", token="X", server="http://x", config_path=p)
    cfg = load_config(p)
    assert cfg["auth"]["test"]["access_token"] == "X"

    rc = cli.main(["-c", p, "logout", "--context", "test"])
    assert rc == 0

    cfg = load_config(p)
    # Token should be gone (key removed or value None).
    token_val = cfg.get("auth", {}).get("test", {}).get("access_token")
    assert token_val is None
