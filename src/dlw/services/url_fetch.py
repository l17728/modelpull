"""SP4e follow-on B: streaming HTTP GET helper. Pre-review R1 B1: body is
read incrementally via aiter_bytes with a running cap, NOT resp.content
(which buffers full body and is a memory-DoS vector)."""
from __future__ import annotations

from dataclasses import dataclass

import httpx

_ALLOWED_CTS = frozenset({
    "text/plain", "text/html", "text/markdown", "text/xml",
    "application/json", "application/xhtml+xml",
})


class FetchError(RuntimeError):
    """Raised when an HTTP GET fails, times out, or returns disallowed content."""


@dataclass
class FetchResponse:
    status_code: int
    content_type: str
    text: str


def _is_allowed_content_type(ct: str) -> bool:
    return ct in _ALLOWED_CTS


async def _http_get(url: str, *, timeout: float, max_bytes: int) -> FetchResponse:
    """Streaming GET capped at max_bytes. follow_redirects=False (security)."""
    async with httpx.AsyncClient(
        follow_redirects=False, timeout=timeout, verify=True,
    ) as client:
        try:
            async with client.stream(
                "GET", url,
                headers={
                    "Accept": "text/*, application/json, application/xhtml+xml",
                    # M1: opt-out of compressed transfer to prevent decompression
                    # amplification before our streaming cap kicks in.
                    "Accept-Encoding": "identity",
                },
            ) as resp:
                ct = (resp.headers.get("content-type", "")
                      .split(";")[0].strip().lower())
                if not _is_allowed_content_type(ct):
                    raise FetchError(
                        f"content_type_rejected: {ct or 'unknown'}")
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    remaining = max_bytes - len(buf)
                    if remaining <= 0:
                        break
                    buf.extend(chunk[:remaining])
                    if len(buf) >= max_bytes:
                        break
                return FetchResponse(
                    status_code=resp.status_code,
                    content_type=ct,
                    text=bytes(buf).decode("utf-8", errors="replace"),
                )
        except httpx.RequestError as e:
            raise FetchError(
                f"request_error: {type(e).__name__}") from e
