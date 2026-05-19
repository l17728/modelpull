"""SDK error hierarchy + exit-code mapping (SP4)."""
from __future__ import annotations

from dlw.sdk import errors as e


def test_hierarchy():
    for c in (e.UsageError, e.NotFound, e.AuthError, e.QuotaExceeded,
              e.Conflict, e.Timeout, e.ApiError):
        assert issubclass(c, e.DlwError)


def test_exit_codes():
    assert e.exit_code_for(e.UsageError("x")) == 2
    assert e.exit_code_for(e.NotFound("x")) == 3
    assert e.exit_code_for(e.AuthError("x")) == 4
    assert e.exit_code_for(e.QuotaExceeded("x")) == 5
    assert e.exit_code_for(e.Conflict("x")) == 6
    assert e.exit_code_for(e.Timeout("x")) == 9
    assert e.exit_code_for(e.ApiError("x")) == 1
    assert e.exit_code_for(e.DlwError("x")) == 1
    assert e.exit_code_for(ValueError("x")) == 1


def test_fields_carried():
    ex = e.Conflict("nope", code="TASK_NOT_TERMINAL", status=409,
                    trace_id="abc", details={"status": "downloading"})
    assert ex.code == "TASK_NOT_TERMINAL"
    assert ex.status == 409
    assert ex.trace_id == "abc"
    assert ex.details == {"status": "downloading"}
    assert str(ex) == "nope"
