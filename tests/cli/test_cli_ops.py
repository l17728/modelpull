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


def test_watch_terminal_exit0(capsys):
    tid = _submit(capsys, "o/ww", "4" * 40)
    # MockTransport /tasks/{id}/stream returns a terminal "succeeded" snapshot.
    assert cli.main(["watch", tid]) == 0


def test_watch_failed_exit1():
    """Unit-test _watch_sse with a fake client whose task_stream yields failed."""
    import argparse
    import contextlib

    from dlw.cli.handlers import _watch_sse

    class _FakeStreamCM:
        def __init__(self):
            self.status_code = 200

        def iter_lines(self):
            import json
            detail = {"id": "fail-task", "repo_id": "o/r",
                      "revision": "a" * 40, "status": "failed",
                      "priority": 1, "created_at": None,
                      "completed_at": None, "error_message": None,
                      "subtasks": []}
            yield ":open"
            yield ""
            yield f"data: {json.dumps(detail)}"
            yield ""

        def read(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    class _FakeTasks:
        def task_stream(self, task_id, *, timeout=None):
            return _FakeStreamCM()

        def get(self, task_id):
            raise AssertionError("should not be called")

    class _FakeClient:
        tasks = _FakeTasks()

    ns = argparse.Namespace(task_id="fail-task", timeout=None, interval=5.0,
                            output="table", quiet=False)
    result = _watch_sse(_FakeClient(), ns, lambda obj, args: None)
    assert result == 1
