"""Opt-in symmetric encryption for the at-rest CLI token (FU8).
Fernet (AES-128-CBC + HMAC) with a PBKDF2-derived key; only `cryptography`."""
from __future__ import annotations

import base64
import os

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from dlw.sdk.errors import TokenDecryptError

_PREFIX = "enc:v1:"
_ITERS = 200_000


def is_encrypted(value: str) -> bool:
    return isinstance(value, str) and value.startswith(_PREFIX)


def _fernet(passphrase: str, salt: bytes) -> Fernet:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=_ITERS)
    return Fernet(base64.urlsafe_b64encode(kdf.derive(passphrase.encode())))


def encrypt_token(plaintext: str, passphrase: str) -> str:
    salt = os.urandom(16)
    tok = _fernet(passphrase, salt).encrypt(plaintext.encode()).decode()
    return f"{_PREFIX}{base64.urlsafe_b64encode(salt).decode()}:{tok}"


def decrypt_token(blob: str, passphrase: str) -> str:
    try:
        _, _, salt_b64, tok = blob.split(":", 3)
        salt = base64.urlsafe_b64decode(salt_b64.encode())
        return _fernet(passphrase, salt).decrypt(tok.encode()).decode()
    except (InvalidToken, ValueError, base64.binascii.Error) as e:
        raise TokenDecryptError("cannot decrypt stored token") from e
