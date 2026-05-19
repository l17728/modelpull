"""dlw list/cancel/delete/watch via httpx.MockTransport (SP4)."""
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


def _submit(capsys, repo, rev):
    cli.main(["-o", "json", "submit", repo, "-r", rev, "-s", "1"])
    return json.loads(capsys.readouterr().out)["id"]


def test_list_json_and_filter(capsys):
    _submit(capsys, "o/l1", "0" * 40)
    rc = cli.main(["-o", "json", "list", "--status", "pending"])
    assert rc == 0
    rows = json.loads(capsys.readouterr().out)
    assert isinstance(rows, list) and all(
        r["status"] == "pending" for r in rows)


def test_list_table_nonempty(capsys):
    _submit(capsys, "o/l2", "1" * 40)
    rc = cli.main(["list"])
    assert rc == 0
    assert "repo_id" in capsys.readouterr().out


def test_cancel_exit0(capsys):
    tid = _submit(capsys, "o/cc", "2" * 40)
    assert cli.main(["cancel", tid]) == 0


def test_delete_non_terminal_exit6(capsys):
    tid = _submit(capsys, "o/dd", "3" * 40)
    assert cli.main(["delete", tid]) == 6


def test_watch_terminal_exit0(capsys, monkeypatch):
    tid = _submit(capsys, "o/ww", "4" * 40)
    # MockTransport keeps a task "pending"; stub TasksAPI.get to flip the
    # status to terminal so watch's poll loop exits. `real` is captured
    # BEFORE monkeypatch so it's the unpatched method.
    from dlw.sdk.client import TasksAPI
    real = TasksAPI.get

    def fake_get(self, task_id):
        t = real(self, task_id)        # real HTTP via MockTransport
        t.status = "cancelled"
        return t
    monkeypatch.setattr(TasksAPI, "get", fake_get)
    assert cli.main(["watch", tid, "--interval", "0"]) == 0
