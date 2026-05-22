"""FU1 context CLI commands (local file, no network)."""
from __future__ import annotations

from dlw.cli import main as cli


def test_set_and_list(capsys, tmp_path):
    p = str(tmp_path / "c.yaml")
    assert cli.main(["-c", p, "context", "set", "dev",
                     "--server", "http://h:8000", "--token", "TK"]) == 0
    assert cli.main(["-c", p, "context", "list"]) == 0
    out = capsys.readouterr().out
    assert "dev" in out and "current" in out.lower()


def test_current_redacts_token(capsys, tmp_path):
    p = str(tmp_path / "c.yaml")
    cli.main(["-c", p, "context", "set", "dev", "--server", "http://h", "--token", "SECRET"])
    capsys.readouterr()
    assert cli.main(["-c", p, "context", "current"]) == 0
    out = capsys.readouterr().out
    assert "SECRET" not in out and "set" in out.lower()


def test_use_switches(capsys, tmp_path):
    p = str(tmp_path / "c.yaml")
    cli.main(["-c", p, "context", "set", "a", "--server", "http://a", "--token", "TA"])
    cli.main(["-c", p, "context", "set", "b", "--server", "http://b", "--token", "TB"])
    assert cli.main(["-c", p, "context", "use", "a"]) == 0


def test_use_missing_exit2(tmp_path):
    p = str(tmp_path / "c.yaml")
    cli.main(["-c", p, "context", "set", "a", "--server", "http://a", "--token", "TA"])
    assert cli.main(["-c", p, "context", "use", "nope"]) == 2
