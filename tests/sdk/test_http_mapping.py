"""raise_for_status maps HTTP+body to typed errors; DownloadTask.from_api."""
from __future__ import annotations

import httpx
import pytest

from dlw.sdk import errors as e
from dlw.sdk._http import raise_for_status
from dlw.sdk.models import TERMINAL, DownloadTask


def _resp(status, json_body=None, text=""):
    if json_body is not None:
        return httpx.Response(status, json=json_body,
                              request=httpx.Request("GET", "http://t/x"))
    return httpx.Response(status, text=text,
                          request=httpx.Request("GET", "http://t/x"))


def test_2xx_no_raise():
    raise_for_status(_resp(200, {"ok": True}))
    raise_for_status(_resp(204))


@pytest.mark.parametrize("status,cls", [
    (404, e.NotFound), (401, e.AuthError), (403, e.AuthError),
    (429, e.QuotaExceeded), (409, e.Conflict), (500, e.ApiError),
])
def test_status_mapping(status, cls):
    with pytest.raises(cls):
        raise_for_status(_resp(status, {"detail": "boom"}))


def test_conflict_code_and_details():
    with pytest.raises(e.Conflict) as ei:
        raise_for_status(_resp(409, {"detail": {
            "code": "TASK_NOT_TERMINAL", "status": "downloading"}}))
    assert ei.value.code == "TASK_NOT_TERMINAL"
    assert ei.value.details == {"status": "downloading"}


def test_quota_code_promotes_even_if_400():
    with pytest.raises(e.QuotaExceeded):
        raise_for_status(_resp(400, {"detail": {"code": "QUOTA_EXCEEDED"}}))


def test_non_json_body_tolerated():
    with pytest.raises(e.ApiError) as ei:
        raise_for_status(_resp(502, text="bad gateway"))
    assert ei.value.status == 502


def test_downloadtask_from_api():
    t = DownloadTask.from_api({
        "id": "11111111-1111-1111-1111-111111111111",
        "repo_id": "o/r", "revision": "abc", "status": "pending",
        "priority": 1, "created_at": "2026-05-19T00:00:00Z",
        "completed_at": None, "error_message": None,
        "subtasks": [{"status": "pending"}]}, api=None)
    assert t.id == "11111111-1111-1111-1111-111111111111"
    assert t.status == "pending" and t.subtasks == [{"status": "pending"}]
    assert "succeeded" in TERMINAL and "failed" in TERMINAL
    assert "cancelled" in TERMINAL
