"""dlw submit / show through the SDK + httpx.MockTransport (SP4; R1)."""
from __future__ import annotations

import json

import pytest

import dlw.cli.main as cli
from tests.sdk._mock import GOOD_TOKEN, make_mock_transport


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    monkeypatch.setattr(cli, "_transport", make_mock_transport())
    monkeypatch.setenv("DLW_TOKEN", GOOD_TOKEN)
    monkeypatch.setenv("DLW_SERVER", "http://mock")
    yield
    monkeypatch.setattr(cli, "_transport", None)


def test_submit_json(capsys):
    rc = cli.main(["-o", "json", "submit", "o/r",
                   "-r", "0" * 40, "-s", "1"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["repo_id"] == "o/r" and out["status"] == "pending"


def test_show_after_submit(capsys):
    cli.main(["-o", "json", "submit", "o/s", "-r", "1" * 40, "-s", "1"])
    tid = json.loads(capsys.readouterr().out)["id"]
    rc = cli.main(["-o", "json", "show", tid])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["id"] == tid


def test_show_missing_exit_3(capsys):
    rc = cli.main(["show", "99999999-9999-9999-9999-999999999999"])
    assert rc == 3
