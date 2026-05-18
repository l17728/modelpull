"""Speed EWMA fusion + controller-side probe (Phase 3 SP2; doc §1.7/§1.8)."""
from __future__ import annotations

import httpx

from dlw.services.source_speed import (
    fuse_ewma,
    pick_probe_size_bytes,
    probe_source_speed,
)
from dlw.sources.base import SourceFile


def test_fuse_no_history_uses_live():
    assert fuse_ewma(live=1000.0, hist=None, hist_weight=0.3) == 1000.0


def test_fuse_blends():
    assert fuse_ewma(live=1000.0, hist=500.0, hist_weight=0.3) == 850.0


def test_probe_size():
    assert pick_probe_size_bytes(probe_size_mb=32) == 32 * 1024 * 1024


class _Drv:
    def download_url(self, f):
        return "https://src/x"

    def auth_token(self, t):
        from dlw.sources.base import SourceToken
        return SourceToken(scheme="none")


async def test_probe_returns_positive_speed():
    transport = httpx.MockTransport(
        lambda r: httpx.Response(206, content=b"x" * 4096))
    bps = await probe_source_speed(
        _Drv(), SourceFile("m", 4096, None, "ref"),
        probe_bytes=4096, timeout_s=5.0, hf_token=None, transport=transport)
    assert bps > 0.0


async def test_probe_failure_returns_zero():
    def boom(r):
        raise httpx.ConnectError("down")
    bps = await probe_source_speed(
        _Drv(), SourceFile("m", 4096, None, "ref"),
        probe_bytes=4096, timeout_s=5.0, hf_token=None,
        transport=httpx.MockTransport(boom))
    assert bps == 0.0
