"""dlw CLI parser skeleton: version, help, usage errors (SP4)."""
from __future__ import annotations

from dlw.cli.main import main


def test_version_exits_zero(capsys):
    assert main(["--version"]) == 0
    assert "dlw" in capsys.readouterr().out


def test_no_command_is_usage_error():
    assert main([]) == 2


def test_unknown_command_is_usage_error():
    assert main(["frobnicate"]) == 2


def test_missing_token_maps_to_exit_2(monkeypatch):
    for v in ("DLW_TOKEN", "DLW_SYSTEM_ADMIN_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    # `list` with no token + no config -> UsageError -> exit 2
    assert main(["--config", "", "list"]) == 2
