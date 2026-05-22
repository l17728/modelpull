"""FU8 — integration tests for encrypt/decrypt seams in _config.py."""
from __future__ import annotations

import pytest

from dlw.sdk._config import load_config, resolve, set_config_value, set_context
from dlw.sdk.errors import UsageError


def _mk(tmp_path):
    """Return a config path string in tmp_path."""
    return str(tmp_path / "c.yaml")


# ---------------------------------------------------------------------------
# set_context with key set → stores encrypted blob, resolve decrypts it
# ---------------------------------------------------------------------------

def test_set_context_encrypts_when_key_set(monkeypatch, tmp_path):
    monkeypatch.setenv("DLW_CONFIG_KEY", "pw")
    p = _mk(tmp_path)
    set_context("dev", server="http://h", token="T1", config_path=p)
    raw = load_config(p)["auth"]["dev"]["access_token"]
    assert raw.startswith("enc:v1:")
    assert raw != "T1"


def test_set_context_resolve_decrypts(monkeypatch, tmp_path):
    monkeypatch.setenv("DLW_CONFIG_KEY", "pw")
    p = _mk(tmp_path)
    set_context("dev", server="http://h", token="T1", make_current=True, config_path=p)
    r = resolve(server=None, token=None, config_path=p)
    assert r.token == "T1"


# ---------------------------------------------------------------------------
# set_config_value with key set → stores encrypted blob, resolve decrypts it
# ---------------------------------------------------------------------------

def test_set_config_value_encrypts_access_token(monkeypatch, tmp_path):
    monkeypatch.setenv("DLW_CONFIG_KEY", "pw")
    p = _mk(tmp_path)
    # Need a current_context for resolve to find the token
    set_context("dev", server="http://h", config_path=p)
    set_config_value("auth.dev.access_token", "T2", config_path=p)
    raw = load_config(p)["auth"]["dev"]["access_token"]
    assert raw.startswith("enc:v1:")
    assert raw != "T2"


def test_set_config_value_resolve_decrypts(monkeypatch, tmp_path):
    monkeypatch.setenv("DLW_CONFIG_KEY", "pw")
    p = _mk(tmp_path)
    set_context("dev", server="http://h", make_current=True, config_path=p)
    set_config_value("auth.dev.access_token", "T2", config_path=p)
    r = resolve(server=None, token=None, config_path=p)
    assert r.token == "T2"


# ---------------------------------------------------------------------------
# set_config_value: no double-encrypt
# ---------------------------------------------------------------------------

def test_set_config_value_no_double_encrypt(monkeypatch, tmp_path):
    monkeypatch.setenv("DLW_CONFIG_KEY", "pw")
    p = _mk(tmp_path)
    set_context("dev", server="http://h", token="T3", make_current=True, config_path=p)
    # The stored value is already enc:v1:...
    raw_first = load_config(p)["auth"]["dev"]["access_token"]
    assert raw_first.startswith("enc:v1:")
    # Set via set_config_value with the already-encrypted value (simulate reading + re-setting)
    # It must NOT double-encrypt
    set_config_value("auth.dev.access_token", raw_first, config_path=p)
    raw_second = load_config(p)["auth"]["dev"]["access_token"]
    assert raw_second.startswith("enc:v1:")
    # Still decryptable
    r = resolve(server=None, token=None, config_path=p)
    assert r.token == "T3"


# ---------------------------------------------------------------------------
# Encrypted on disk + key UNSET → UsageError
# ---------------------------------------------------------------------------

def test_resolve_encrypted_key_unset_raises(monkeypatch, tmp_path):
    p = _mk(tmp_path)
    # Write encrypted token while key is set
    monkeypatch.setenv("DLW_CONFIG_KEY", "pw")
    set_context("dev", server="http://h", token="T4", make_current=True, config_path=p)
    # Now unset the key
    monkeypatch.delenv("DLW_CONFIG_KEY")
    with pytest.raises(UsageError, match="DLW_CONFIG_KEY is not set"):
        resolve(server=None, token=None, config_path=p)


# ---------------------------------------------------------------------------
# Encrypted on disk + WRONG key → UsageError
# ---------------------------------------------------------------------------

def test_resolve_encrypted_wrong_key_raises(monkeypatch, tmp_path):
    p = _mk(tmp_path)
    monkeypatch.setenv("DLW_CONFIG_KEY", "correct")
    set_context("dev", server="http://h", token="T5", make_current=True, config_path=p)
    monkeypatch.setenv("DLW_CONFIG_KEY", "wrong")
    with pytest.raises(UsageError, match="wrong DLW_CONFIG_KEY"):
        resolve(server=None, token=None, config_path=p)


# ---------------------------------------------------------------------------
# Backward compat: with key UNSET, set_context stores plaintext + resolve works
# ---------------------------------------------------------------------------

def test_backward_compat_plaintext_no_key(monkeypatch, tmp_path):
    # autouse fixture already deletes DLW_CONFIG_KEY; be explicit
    monkeypatch.delenv("DLW_CONFIG_KEY", raising=False)
    p = _mk(tmp_path)
    set_context("dev2", server="http://h", token="P", make_current=True, config_path=p)
    raw = load_config(p)["auth"]["dev2"]["access_token"]
    assert raw == "P"
    r = resolve(server=None, token=None, config_path=p)
    assert r.token == "P"


# ---------------------------------------------------------------------------
# set_config_value: defaults.* keys are NOT encrypted even when key is set
# ---------------------------------------------------------------------------

def test_set_config_value_defaults_not_encrypted(monkeypatch, tmp_path):
    monkeypatch.setenv("DLW_CONFIG_KEY", "pw")
    p = _mk(tmp_path)
    set_config_value("defaults.storage_id", 7, config_path=p)
    raw = load_config(p)["defaults"]["storage_id"]
    assert raw == 7
