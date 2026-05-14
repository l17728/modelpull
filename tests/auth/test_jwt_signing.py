"""Tests for dlw.auth.jwt_signing (Phase 2 W3a §3.2)."""
from __future__ import annotations

import time

import jwt as _pyjwt
import pytest

from dlw.auth.jwt_signing import bootstrap_keypair, sign, verify


def test_bootstrap_keypair_idempotent(tmp_path) -> None:
    kp1 = bootstrap_keypair(tmp_path)
    kp2 = bootstrap_keypair(tmp_path)
    assert kp1.priv_pem == kp2.priv_pem
    assert kp1.pub_pem == kp2.pub_pem


def test_sign_and_verify_roundtrip(tmp_path) -> None:
    kp = bootstrap_keypair(tmp_path)
    token = sign(kp, executor_id="host-1-worker-1", epoch=3,
                 scopes=["heartbeat", "poll"], ttl_seconds=3600)
    claims = verify(kp, token)
    assert claims["sub"] == "host-1-worker-1"
    assert claims["epoch"] == 3
    assert claims["scope"] == "heartbeat poll"
    assert claims["iss"] == "dlw-controller"


def test_verify_rejects_expired_token(tmp_path) -> None:
    kp = bootstrap_keypair(tmp_path)
    token = sign(kp, executor_id="e", epoch=1, scopes=["heartbeat"],
                 ttl_seconds=-10)
    with pytest.raises(_pyjwt.PyJWTError):
        verify(kp, token)


def test_verify_rejects_wrong_issuer(tmp_path) -> None:
    kp = bootstrap_keypair(tmp_path)
    now = int(time.time())
    bad = _pyjwt.encode(
        {"iss": "evil", "sub": "e", "epoch": 1, "scope": "heartbeat",
         "iat": now, "exp": now + 3600},
        kp.priv_pem.decode("utf-8"), algorithm="EdDSA",
    )
    with pytest.raises(_pyjwt.PyJWTError):
        verify(kp, bad)
