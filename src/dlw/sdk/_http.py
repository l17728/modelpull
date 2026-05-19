"""Map an httpx error response to a typed dlw.sdk error."""
from __future__ import annotations

import httpx

from dlw.sdk import errors as e


def raise_for_status(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    code = message = trace = None
    details: dict = {}
    try:
        body = resp.json()
    except Exception:
        body = None
    if isinstance(body, dict):
        d = body.get("detail", body)
        if isinstance(d, dict):
            code = d.get("code")
            message = d.get("message") or d.get("detail")
            trace = d.get("trace_id")
            details = d.get("details") or {
                k: v for k, v in d.items()
                if k not in ("code", "message", "trace_id")}
        elif isinstance(d, str):
            message = d
    if message is None:
        message = (resp.text or f"HTTP {resp.status_code}")[:500]
    s = resp.status_code
    if s == 404:
        cls: type[e.DlwError] = e.NotFound
    elif s in (401, 403):
        cls = e.AuthError
    elif s == 429 or code == "QUOTA_EXCEEDED":
        cls = e.QuotaExceeded
    elif s == 409:
        cls = e.Conflict
    else:
        cls = e.ApiError
    raise cls(message, code=code, status=s, trace_id=trace, details=details)
