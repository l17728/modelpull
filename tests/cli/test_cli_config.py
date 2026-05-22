"""FU7 dlw config get/set/unset/list (dotted keys, token-redacted) + defaults wiring."""
from __future__ import annotations

import json as _json

import httpx
import pytest
import yaml

import dlw.cli.main as cli

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _capture_transport(captured: list):
    def _h(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path == "/api/v1/tasks":
            captured.append(_json.loads(req.content))
            return httpx.Response(201, json={
                "id": "t1", "repo_id": "repo/x", "revision": "main",
                "status": "queued", "priority": 1,
                "created_at": "2026-01-01T00:00:00Z",
                "completed_at": None, "error_message": None})
        return httpx.Response(200, json={"items": []})
    return httpx.MockTransport(_h)


# ---------------------------------------------------------------------------
# M1: config get/set/unset/list
# ---------------------------------------------------------------------------

def test_set_get_roundtrip_int(capsys, tmp_path):
    p = str(tmp_path / "c.yaml")
    # set
    assert cli.main(["-c", p, "config", "set", "defaults.storage_id", "5"]) == 0
    capsys.readouterr()
    # get
    assert cli.main(["-c", p, "config", "get", "defaults.storage_id"]) == 0
    out = capsys.readouterr().out
    assert "5" in out
    # check yaml on disk: must be int, not "5"
    cfg = yaml.safe_load(open(p, encoding="utf-8").read())
    assert cfg["defaults"]["storage_id"] == 5
    assert isinstance(cfg["defaults"]["storage_id"], int)


def test_set_get_string(capsys, tmp_path):
    p = str(tmp_path / "c.yaml")
    assert cli.main(["-c", p, "config", "set",
                     "defaults.source_strategy", "round_robin"]) == 0
    capsys.readouterr()
    assert cli.main(["-c", p, "config", "get", "defaults.source_strategy"]) == 0
    out = capsys.readouterr().out
    assert "round_robin" in out


def test_unset(capsys, tmp_path):
    p = str(tmp_path / "c.yaml")
    cli.main(["-c", p, "config", "set", "defaults.priority", "3"])
    capsys.readouterr()
    # unset
    assert cli.main(["-c", p, "config", "unset", "defaults.priority"]) == 0
    capsys.readouterr()
    # get should print nothing
    assert cli.main(["-c", p, "config", "get", "defaults.priority"]) == 0
    out = capsys.readouterr().out
    assert out.strip() == ""


def test_list_shows_keys(capsys, tmp_path):
    p = str(tmp_path / "c.yaml")
    cli.main(["-c", p, "config", "set", "defaults.storage_id", "7"])
    cli.main(["-c", p, "config", "set", "defaults.output", "json"])
    capsys.readouterr()
    assert cli.main(["-c", p, "config", "list"]) == 0
    out = capsys.readouterr().out
    assert "defaults.storage_id" in out
    assert "7" in out


def test_token_redacted(capsys, tmp_path):
    p = str(tmp_path / "c.yaml")
    cli.main(["-c", p, "config", "set", "auth.prod.access_token", "SECRET"])
    capsys.readouterr()
    # get should NOT print SECRET
    assert cli.main(["-c", p, "config", "get", "auth.prod.access_token"]) == 0
    out = capsys.readouterr().out
    assert "SECRET" not in out
    # list should NOT print SECRET
    assert cli.main(["-c", p, "config", "list"]) == 0
    out = capsys.readouterr().out
    assert "SECRET" not in out
    # getting the PARENT dict must not leak the token either (recursive redact)
    assert cli.main(["-c", p, "config", "get", "auth.prod"]) == 0
    out = capsys.readouterr().out
    assert "SECRET" not in out


# ---------------------------------------------------------------------------
# M2: submit defaults wiring
# ---------------------------------------------------------------------------

def test_submit_uses_default_storage(monkeypatch, tmp_path):
    p = str(tmp_path / "c.yaml")
    cli.main(["-c", p, "config", "set", "defaults.storage_id", "7"])
    captured = []
    monkeypatch.setattr(cli, "_transport", _capture_transport(captured))
    monkeypatch.setenv("DLW_TOKEN", "x")
    rc = cli.main(["-c", p, "submit", "repo/x", "-r", "main"])
    assert rc == 0
    assert captured[0]["storage_id"] == 7


def test_submit_flag_beats_default(monkeypatch, tmp_path):
    p = str(tmp_path / "c.yaml")
    cli.main(["-c", p, "config", "set", "defaults.storage_id", "7"])
    captured = []
    monkeypatch.setattr(cli, "_transport", _capture_transport(captured))
    monkeypatch.setenv("DLW_TOKEN", "x")
    rc = cli.main(["-c", p, "submit", "repo/x", "-r", "main", "-s", "9"])
    assert rc == 0
    assert captured[0]["storage_id"] == 9


def test_submit_missing_storage_errors(monkeypatch, tmp_path):
    p = str(tmp_path / "c.yaml")
    # no default, no --storage
    captured = []
    monkeypatch.setattr(cli, "_transport", _capture_transport(captured))
    monkeypatch.setenv("DLW_TOKEN", "x")
    rc = cli.main(["-c", p, "submit", "repo/x", "-r", "main"])
    assert rc == 2


def test_output_default_json(monkeypatch, capsys, tmp_path):
    p = str(tmp_path / "c.yaml")
    monkeypatch.delenv("DLW_OUTPUT", raising=False)
    cli.main(["-c", p, "config", "set", "defaults.output", "json"])
    capsys.readouterr()
    captured = []
    monkeypatch.setattr(cli, "_transport", _capture_transport(captured))
    monkeypatch.setenv("DLW_TOKEN", "x")
    rc = cli.main(["-c", p, "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.strip().startswith("[")


def test_resolve_output_flag_wins(monkeypatch, tmp_path):
    """_resolve_output: explicit flag always wins."""
    from dlw.cli.main import _resolve_output
    monkeypatch.delenv("DLW_OUTPUT", raising=False)
    p = str(tmp_path / "c.yaml")
    cli.main(["-c", p, "config", "set", "defaults.output", "json"])
    # flag=table beats config=json
    assert _resolve_output("table", p) == "table"


def test_resolve_output_env_beats_config(monkeypatch, tmp_path):
    """_resolve_output: env beats config."""
    from dlw.cli.main import _resolve_output
    monkeypatch.setenv("DLW_OUTPUT", "json")
    p = str(tmp_path / "c.yaml")
    cli.main(["-c", p, "config", "set", "defaults.output", "table"])
    # flag=None, env=json → json
    assert _resolve_output(None, p) == "json"


def test_resolve_output_unknown_falls_back_to_table(monkeypatch, tmp_path):
    """_resolve_output: unknown value (e.g. 'yaml') → 'table'."""
    from dlw.cli.main import _resolve_output
    monkeypatch.delenv("DLW_OUTPUT", raising=False)
    p = str(tmp_path / "c.yaml")
    cli.main(["-c", p, "config", "set", "defaults.output", "yaml"])
    assert _resolve_output(None, p) == "table"


def test_resolve_output_all_none_gives_table(monkeypatch, tmp_path):
    """_resolve_output: all None → 'table'."""
    from dlw.cli.main import _resolve_output
    monkeypatch.delenv("DLW_OUTPUT", raising=False)
    p = str(tmp_path / "c.yaml")
    assert _resolve_output(None, p) == "table"


# ---------------------------------------------------------------------------
# M3: FU8 dlw config encrypt
# ---------------------------------------------------------------------------

def test_config_encrypt_migrates_plaintext(monkeypatch, capsys, tmp_path):
    """config encrypt migrates a plaintext token to enc:v1: and resolve works."""
    import yaml

    from dlw.sdk._config import load_config, resolve

    p = str(tmp_path / "c.yaml")
    # Seed plaintext token with key UNSET (autouse fixture already deletes it)
    assert cli.main(["-c", p, "config", "set", "auth.prod.access_token", "PLAINTOK"]) == 0
    # Also set a current context so resolve() knows where to look
    assert cli.main(["-c", p, "context", "set", "prod", "--server", "http://h"]) == 0
    capsys.readouterr()

    # Now set the key and run config encrypt
    monkeypatch.setenv("DLW_CONFIG_KEY", "pw")
    rc = cli.main(["-c", p, "config", "encrypt"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "1" in out  # at least one token migrated

    # Raw on-disk value is now encrypted
    raw = load_config(p)["auth"]["prod"]["access_token"]
    assert raw.startswith("enc:v1:")

    # resolve() with key set returns original plaintext
    r = resolve(server=None, token=None, config_path=p)
    assert r.token == "PLAINTOK"


def test_config_encrypt_requires_key(monkeypatch, capsys, tmp_path):
    """config encrypt without DLW_CONFIG_KEY exits 2."""
    p = str(tmp_path / "c.yaml")
    monkeypatch.delenv("DLW_CONFIG_KEY", raising=False)
    rc = cli.main(["-c", p, "config", "encrypt"])
    assert rc == 2


def test_config_encrypt_skips_already_encrypted(monkeypatch, capsys, tmp_path):
    """config encrypt is idempotent: running twice reports 0 migrated on second run."""
    p = str(tmp_path / "c.yaml")
    # Write an encrypted token directly (key set)
    monkeypatch.setenv("DLW_CONFIG_KEY", "pw")
    assert cli.main(["-c", p, "config", "set", "auth.prod.access_token", "PLAINTOK"]) == 0
    capsys.readouterr()

    # First encrypt — nothing to migrate (already encrypted by config set)
    rc = cli.main(["-c", p, "config", "encrypt"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "0" in out  # zero migrated
