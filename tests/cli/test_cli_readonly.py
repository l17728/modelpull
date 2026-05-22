"""dlw whoami/quota/exec list/events/audit via httpx.MockTransport (SP4)."""
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


def test_whoami(capsys):
    assert cli.main(["whoami"]) == 0
    out = capsys.readouterr().out
    assert "tenant_admin" in out


def test_quota_json(capsys):
    assert cli.main(["-o", "json", "quota"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["bytes_quota_month"] == 1000


def test_exec_list(capsys):
    assert cli.main(["exec", "list"]) == 0
    assert "ex-1" in capsys.readouterr().out


def test_events(capsys):
    assert cli.main(["events", "11111111-1111-1111-1111-111111111111"]) == 0
    assert "task.created" in capsys.readouterr().out


def test_audit(capsys):
    assert cli.main(["audit", "--action", "task."]) == 0
    assert "task.created" in capsys.readouterr().out


def test_events_follow_404(capsys):
    """events --follow against a 404 task → NotFound → exit 3."""
    # The mock returns 404 for /api/v1/tasks/<unknown>/events/stream
    # because there is no stream route registered; the handler falls to 404.
    rc = cli.main(["events", "00000000-0000-0000-0000-000000000000", "--follow"])
    assert rc == 3
