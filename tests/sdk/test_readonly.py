"""SP4 CLI read-only SDK methods (sync, via MockTransport)."""
from __future__ import annotations

import pytest

from dlw.sdk.client import Client
from tests.sdk._mock import make_mock_transport


@pytest.fixture
def sync_client():
    return Client(server="http://mock", token="good",
                  transport=make_mock_transport())


def test_me(sync_client):
    assert sync_client.me()["role"] == "tenant_admin"


def test_quota(sync_client):
    q = sync_client.quota.current()
    assert q["bytes_quota_month"] == 1000 and "storage_gb_used" in q


def test_executors_list(sync_client):
    items = sync_client.executors.list()["items"]
    assert items and items[0]["id"] == "ex-1"


def test_task_events(sync_client):
    ev = sync_client.tasks.events("11111111-1111-1111-1111-111111111111")
    assert ev["items"][0]["type"] == "task.created"


def test_audit_search(sync_client):
    a = sync_client.audit.search(action="task.")
    assert a["items"][0]["outcome"] == "success"


def test_task_stream_buffered(sync_client):
    lines = []
    with sync_client.tasks.task_stream("33333333-3333-3333-3333-333333333333") as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            lines.append(line)
    data = [l for l in lines if l.startswith("data: ")]
    assert data and '"status": "succeeded"' in data[0]
