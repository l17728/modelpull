"""list (client-side filter) + cancel + delete + error mapping (SP4)."""
from __future__ import annotations

import pytest

from dlw.sdk import errors as e
from dlw.sdk.client import Client
from tests.sdk._mock import GOOD_TOKEN, make_mock_transport


def _client(token=GOOD_TOKEN):
    return Client(server="http://mock", token=token,
                  transport=make_mock_transport())


def test_list_and_status_filter():
    with _client() as c:
        a = c.tasks.submit(repo_id="o/a", revision="0" * 40, storage_id=1)
        c.tasks.submit(repo_id="o/b", revision="1" * 40, storage_id=1)
        allt = c.tasks.list()
        assert {t.repo_id for t in allt} >= {"o/a", "o/b"}
        assert all(t.status == "pending"
                   for t in c.tasks.list(status="pending"))
        assert c.tasks.list(status="cancelled") == []
        assert len(c.tasks.list(limit=1)) == 1
        assert any(t.id == a.id for t in allt)


def test_cancel_sets_cancelling():
    with _client() as c:
        t = c.tasks.submit(repo_id="o/c", revision="2" * 40, storage_id=1)
        c.tasks.cancel(t.id, reason="user")
        # R3: cancel_task only ever sets "cancelling" synchronously.
        assert c.tasks.get(t.id).status == "cancelling"


def test_delete_non_terminal_raises_conflict():
    with _client() as c:
        t = c.tasks.submit(repo_id="o/d", revision="3" * 40, storage_id=1)
        with pytest.raises(e.Conflict) as ei:
            c.tasks.delete(t.id)        # still pending -> 409
        assert ei.value.code == "TASK_NOT_TERMINAL"


def test_get_missing_raises_notfound():
    with _client() as c:
        with pytest.raises(e.NotFound):
            c.tasks.get("99999999-9999-9999-9999-999999999999")


def test_bad_token_raises_autherror():
    with _client(token="wrong") as c:
        with pytest.raises(e.AuthError):
            c.tasks.list()


def test_missing_token_is_usage_error(monkeypatch):
    for v in ("DLW_TOKEN", "DLW_SYSTEM_ADMIN_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    with pytest.raises(e.UsageError):
        Client(server="http://test", token=None, config_path="")
