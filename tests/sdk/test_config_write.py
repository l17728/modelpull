"""FU1 config write-back round-trip."""
from __future__ import annotations

import pytest

from dlw.sdk._config import load_config, resolve, save_config, set_context, use_context
from dlw.sdk.errors import UsageError


def test_set_context_roundtrip(tmp_path):
    p = str(tmp_path / "config.yaml")
    set_context("dev", server="http://h:8000/", token="T1", config_path=p)
    r = resolve(server=None, token=None, config_path=p)
    assert r.server == "http://h:8000" and r.token == "T1"


def test_use_context_switches(tmp_path):
    p = str(tmp_path / "config.yaml")
    set_context("a", server="http://a", token="TA", config_path=p)
    set_context("b", server="http://b", token="TB", make_current=False,
                config_path=p)
    assert resolve(server=None, token=None, config_path=p).server == "http://a"
    use_context("b", config_path=p)
    assert resolve(server=None, token=None, config_path=p).server == "http://b"


def test_use_missing_context_errors(tmp_path):
    p = str(tmp_path / "config.yaml")
    set_context("a", server="http://a", token="TA", config_path=p)
    with pytest.raises(UsageError):
        use_context("nope", config_path=p)


def test_save_creates_parent_dir(tmp_path):
    import os
    p = str(tmp_path / "nested" / "dir" / "config.yaml")
    save_config({"current_context": "x"}, config_path=p)
    assert os.path.isfile(p)


def test_load_config_public_alias(tmp_path):
    p = str(tmp_path / "config.yaml")
    set_context("a", server="http://a", token="TA", config_path=p)
    cfg = load_config(p)
    assert cfg["current_context"] == "a" and cfg["contexts"]["a"]["server"] == "http://a"
