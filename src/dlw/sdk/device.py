"""Token-free RFC 8628 device-flow bootstrap (FU6). Used by `dlw login`
before any token exists — does NOT use Client (which requires a token)."""
from __future__ import annotations

import time

import httpx

from dlw.sdk._http import raise_for_status
from dlw.sdk.errors import Timeout, UsageError


def _client(server, transport, timeout):
    return httpx.Client(base_url=server.rstrip("/"), timeout=timeout,
                        transport=transport)


def device_authorize(server, *, transport=None, timeout=30.0) -> dict:
    with _client(server, transport, timeout) as h:
        r = h.post("/api/v1/auth/device")
        raise_for_status(r)
        return r.json()


def device_token(server, device_code, *, transport=None, timeout=30.0) -> dict:
    with _client(server, transport, timeout) as h:
        r = h.post("/api/v1/auth/device/token", json={"device_code": device_code})
        if r.status_code in (200, 400):
            return r.json()
        raise_for_status(r)
        return r.json()


def poll_for_token(server, device_code, *, interval, deadline,
                   sleep=time.sleep, token_fn=device_token, transport=None) -> dict:
    iv = interval
    while True:
        body = token_fn(server, device_code, transport=transport)
        if body.get("access_token"):
            return body
        err = body.get("error")
        if err in ("access_denied", "expired_token", "invalid_grant"):
            raise UsageError(f"device login failed: {err}")
        if err == "slow_down":
            iv += 5
        # authorization_pending or any unexpected/unknown error → keep polling
        # until the deadline (don't spin forever silently — bounded by deadline).
        if time.monotonic() >= deadline:
            raise Timeout("device login timed out")
        sleep(iv)
