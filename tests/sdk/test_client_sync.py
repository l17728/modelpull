"""sync Client.tasks.submit/get via httpx.MockTransport (SP4; R1)."""
from __future__ import annotations

import pytest

from dlw.sdk import errors as e
from dlw.sdk.client import Client
from tests.sdk._mock import GOOD_TOKEN, make_mock_transport


def _client():
    return Client(server="http://mock", token=GOOD_TOKEN,
                  transport=make_mock_transport())


def test_submit_then_get():
    with _client() as c:
        t = c.tasks.submit(repo_id="o/r", revision="0" * 40, storage_id=1)
        assert t.status == "pending" and t.repo_id == "o/r" and t.id
        got = c.tasks.get(t.id)
        assert got.id == t.id
        assert len(got.subtasks) == 2          # TaskDetail shape
        assert got.refresh().id == t.id


def test_submit_requires_storage():
    with _client() as c:
        with pytest.raises(TypeError):
            c.tasks.submit(repo_id="o/r", revision="0" * 40)  # no storage_id


def test_bad_token_is_auth_error():
    with Client(server="http://mock", token="wrong",
                transport=make_mock_transport()) as c:
        with pytest.raises(e.AuthError):
            c.tasks.list()
