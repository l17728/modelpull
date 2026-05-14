"""HMAC heartbeat: nonce store + signature verify (Phase 2 W3a §3.3)."""
from __future__ import annotations

import hashlib
import hmac as _hmac
import time
from collections import OrderedDict


class NonceStore:
    """In-process LRU with timestamp-based eviction. asyncio single-threaded —
    no lock needed. Restart loses state; replay defense is bounded by the
    ±5min timestamp window enforced at the dependency layer."""

    def __init__(self, *, maxsize: int = 10_000, ttl_seconds: int = 300) -> None:
        self._maxsize = maxsize
        self._ttl = ttl_seconds
        self._data: OrderedDict[str, float] = OrderedDict()

    def _evict_expired(self) -> None:
        cutoff = time.monotonic() - self._ttl
        while self._data:
            k, v = next(iter(self._data.items()))
            if v >= cutoff:
                break
            self._data.popitem(last=False)

    def seen(self, nonce: str) -> bool:
        self._evict_expired()
        return nonce in self._data

    def add(self, nonce: str) -> None:
        self._evict_expired()
        if len(self._data) >= self._maxsize:
            self._data.popitem(last=False)
        self._data[nonce] = time.monotonic()


def compute_hmac(hmac_seed: bytes, *, ts: int, nonce: str, body: bytes) -> str:
    """HMAC-SHA256(hmac_seed, f'{ts}:{nonce}:'.encode() + body). Hex string."""
    msg = f"{ts}:{nonce}:".encode("utf-8") + body
    return _hmac.new(hmac_seed, msg, hashlib.sha256).hexdigest()


def verify_hmac(hmac_seed: bytes, *, ts: int, nonce: str, body: bytes,
                signature_hex: str) -> bool:
    expected = compute_hmac(hmac_seed, ts=ts, nonce=nonce, body=body)
    return _hmac.compare_digest(expected, signature_hex)
