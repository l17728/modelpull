"""Tests for dlw.auth.hmac_nonce (Phase 2 W3a §3.3)."""
from __future__ import annotations

from dlw.auth.hmac_nonce import NonceStore, compute_hmac, verify_hmac


_SEED = b"\x01" * 32


def test_hmac_compute_and_verify_roundtrip() -> None:
    body = b'{"health_score":100}'
    sig = compute_hmac(_SEED, ts=1715739200, nonce="abc", body=body)
    assert verify_hmac(_SEED, ts=1715739200, nonce="abc", body=body,
                       signature_hex=sig)


def test_hmac_verify_rejects_tampered_body() -> None:
    body = b'{"health_score":100}'
    sig = compute_hmac(_SEED, ts=1715739200, nonce="abc", body=body)
    tampered = b'{"health_score":101}'
    assert not verify_hmac(_SEED, ts=1715739200, nonce="abc", body=tampered,
                           signature_hex=sig)


def test_nonce_store_first_add_then_seen() -> None:
    store = NonceStore(maxsize=100, ttl_seconds=300)
    assert not store.seen("n1")
    store.add("n1")
    assert store.seen("n1")


def test_nonce_store_evicts_after_ttl(monkeypatch) -> None:
    store = NonceStore(maxsize=100, ttl_seconds=10)
    fake = [1000.0]
    monkeypatch.setattr("dlw.auth.hmac_nonce.time.monotonic", lambda: fake[0])
    store.add("n1")
    assert store.seen("n1")
    fake[0] += 11
    assert not store.seen("n1")
