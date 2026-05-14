"""Ed25519 JWT signing for executor JWTs (Phase 2 W3a §3.2)."""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jwt as _pyjwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


@dataclass(frozen=True)
class JWTKeypair:
    priv_pem: bytes
    pub_pem: bytes


def bootstrap_keypair(ca_dir: Path) -> JWTKeypair:
    """Idempotent: load or generate jwt-signing.pem (chmod 600, PKCS8 Ed25519)."""
    priv_path = ca_dir / "jwt-signing.pem"
    if priv_path.exists():
        priv_pem = priv_path.read_bytes()
        priv = serialization.load_pem_private_key(priv_pem, password=None)
        if not isinstance(priv, ed25519.Ed25519PrivateKey):
            raise ValueError("JWT signing key is not Ed25519")
    else:
        priv = ed25519.Ed25519PrivateKey.generate()
        priv_pem = priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        ca_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        priv_path.write_bytes(priv_pem)
        priv_path.chmod(0o600)
    pub_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return JWTKeypair(priv_pem=priv_pem, pub_pem=pub_pem)


def sign(kp: JWTKeypair, *, executor_id: str, epoch: int,
         scopes: list[str], ttl_seconds: int = 3600) -> str:
    """Sign an executor JWT. Returns compact JWS."""
    now = int(time.time())
    claims = {
        "iss": "dlw-controller",
        "sub": executor_id,
        "epoch": epoch,
        "scope": " ".join(scopes),
        "iat": now,
        "exp": now + ttl_seconds,
    }
    return _pyjwt.encode(claims, kp.priv_pem.decode("utf-8"), algorithm="EdDSA")


def verify(kp: JWTKeypair, token: str) -> dict[str, Any]:
    """Decode + verify. Raises jwt.PyJWTError on any failure."""
    return _pyjwt.decode(
        token, kp.pub_pem.decode("utf-8"),
        algorithms=["EdDSA"],
        issuer="dlw-controller",
        options={"require": ["sub", "epoch", "scope", "exp", "iss", "iat"]},
    )
