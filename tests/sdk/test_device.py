"""Tests for token-free RFC 8628 device-flow SDK bootstrap (FU6)."""
from __future__ import annotations

import time

import httpx
import pytest

from dlw.sdk.device import device_authorize, device_token, poll_for_token
from dlw.sdk.errors import Timeout, UsageError


def _make_transport(handler):
    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# device_authorize
# ---------------------------------------------------------------------------

def test_device_authorize_returns_body():
    body = {
        "device_code": "dc123",
        "user_code": "BCDF-GHJK",
        "verification_uri": "/device",
        "verification_uri_complete": "/device?user_code=BCDF-GHJK",
        "expires_in": 600,
        "interval": 5,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/auth/device"
        return httpx.Response(200, json=body)

    transport = _make_transport(handler)
    result = device_authorize("http://mock", transport=transport)
    assert result == body


def test_device_authorize_raises_on_500():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "internal error"})

    transport = _make_transport(handler)
    from dlw.sdk.errors import ApiError
    with pytest.raises(ApiError):
        device_authorize("http://mock", transport=transport)


# ---------------------------------------------------------------------------
# device_token
# ---------------------------------------------------------------------------

def test_device_token_returns_body_on_400_pending():
    body = {"error": "authorization_pending"}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/auth/device/token"
        return httpx.Response(400, json=body)

    transport = _make_transport(handler)
    result = device_token("http://mock", "dc123", transport=transport)
    assert result == body


def test_device_token_returns_body_on_200():
    body = {"access_token": "JWT", "token_type": "Bearer",
            "expires_in": 3600, "tenant_id": 1, "role": "tenant_operator"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    transport = _make_transport(handler)
    result = device_token("http://mock", "dc123", transport=transport)
    assert result == body


def test_device_token_raises_on_500():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "server error"})

    transport = _make_transport(handler)
    from dlw.sdk.errors import ApiError
    with pytest.raises(ApiError):
        device_token("http://mock", "dc123", transport=transport)


# ---------------------------------------------------------------------------
# poll_for_token
# ---------------------------------------------------------------------------

def test_poll_for_token_returns_on_success():
    calls = []

    def fake_token_fn(server, device_code, *, transport=None):
        calls.append(len(calls))
        if len(calls) < 3:
            return {"error": "authorization_pending"}
        return {"access_token": "GOOD_JWT", "token_type": "Bearer",
                "expires_in": 3600, "tenant_id": 1, "role": "tenant_operator"}

    result = poll_for_token(
        "http://mock", "dc123",
        interval=0,
        deadline=time.monotonic() + 10,
        sleep=lambda _: None,
        token_fn=fake_token_fn,
    )
    assert result["access_token"] == "GOOD_JWT"
    assert len(calls) == 3


def test_poll_for_token_raises_usage_error_on_access_denied():
    def fake_token_fn(server, device_code, *, transport=None):
        return {"error": "access_denied"}

    with pytest.raises(UsageError, match="access_denied"):
        poll_for_token(
            "http://mock", "dc123",
            interval=0,
            deadline=time.monotonic() + 10,
            sleep=lambda _: None,
            token_fn=fake_token_fn,
        )


def test_poll_for_token_raises_usage_error_on_expired_token():
    def fake_token_fn(server, device_code, *, transport=None):
        return {"error": "expired_token"}

    with pytest.raises(UsageError, match="expired_token"):
        poll_for_token(
            "http://mock", "dc123",
            interval=0,
            deadline=time.monotonic() + 10,
            sleep=lambda _: None,
            token_fn=fake_token_fn,
        )


def test_poll_for_token_raises_usage_error_on_invalid_grant():
    def fake_token_fn(server, device_code, *, transport=None):
        return {"error": "invalid_grant"}

    with pytest.raises(UsageError, match="invalid_grant"):
        poll_for_token(
            "http://mock", "dc123",
            interval=0,
            deadline=time.monotonic() + 10,
            sleep=lambda _: None,
            token_fn=fake_token_fn,
        )


def test_poll_for_token_raises_timeout_on_deadline_exceeded():
    # Deadline is already in the past.
    def fake_token_fn(server, device_code, *, transport=None):
        return {"error": "authorization_pending"}

    with pytest.raises(Timeout):
        poll_for_token(
            "http://mock", "dc123",
            interval=0,
            deadline=time.monotonic() - 1,
            sleep=lambda _: None,
            token_fn=fake_token_fn,
        )


def test_poll_for_token_slow_down_increases_interval():
    """slow_down causes interval to increase; we verify poll eventually succeeds."""
    call_count = [0]

    def fake_token_fn(server, device_code, *, transport=None):
        call_count[0] += 1
        if call_count[0] == 1:
            return {"error": "slow_down"}
        return {"access_token": "JWT2", "token_type": "Bearer",
                "expires_in": 3600, "tenant_id": 1, "role": "tenant_operator"}

    slept = []
    result = poll_for_token(
        "http://mock", "dc123",
        interval=0,
        deadline=time.monotonic() + 30,
        sleep=slept.append,
        token_fn=fake_token_fn,
    )
    assert result["access_token"] == "JWT2"
    # After slow_down interval should have increased by 5
    assert slept[0] == 5
