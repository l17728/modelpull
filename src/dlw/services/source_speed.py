"""Source speed: controller-side probe + EWMA fusion (Phase 3 SP2)."""
from __future__ import annotations

import time
from typing import Any

import httpx


def fuse_ewma(*, live: float, hist: float | None,
              hist_weight: float) -> float:
    if hist is None:
        return live
    return (1.0 - hist_weight) * live + hist_weight * hist


def pick_probe_size_bytes(*, probe_size_mb: int) -> int:
    return probe_size_mb * 1024 * 1024


async def probe_source_speed(
    driver: Any, file: Any, *, probe_bytes: int, timeout_s: float,
    hf_token: str | None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> float:
    """One ranged GET (controller->source) timing bytes/sec. 0.0 on any
    failure (effect: that source is treated as unavailable for this task)."""
    url = driver.download_url(file)
    tok = driver.auth_token(hf_token)
    headers = {"Range": f"bytes=0-{max(0, probe_bytes - 1)}"}
    if tok.scheme == "bearer" and tok.value:
        headers["Authorization"] = f"Bearer {tok.value}"
    try:
        async with httpx.AsyncClient(timeout=timeout_s, transport=transport,
                                     follow_redirects=True) as c:
            start = time.monotonic()
            recv = 0
            async with c.stream("GET", url, headers=headers) as resp:
                if resp.status_code >= 400:
                    return 0.0
                async for buf in resp.aiter_bytes(64 * 1024):
                    recv += len(buf)
            elapsed = time.monotonic() - start
        if recv <= 0:
            return 0.0
        return recv / max(elapsed, 1e-9)
    except Exception:
        return 0.0
