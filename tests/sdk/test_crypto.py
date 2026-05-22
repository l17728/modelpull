"""FU8 — unit tests for src/dlw/sdk/_crypto.py."""
from __future__ import annotations

import pytest

from dlw.sdk.errors import TokenDecryptError


def test_round_trip():
    from dlw.sdk._crypto import decrypt_token, encrypt_token

    assert decrypt_token(encrypt_token("tok", "pw"), "pw") == "tok"


def test_is_encrypted_true_for_blob():
    from dlw.sdk._crypto import encrypt_token, is_encrypted

    assert is_encrypted(encrypt_token("tok", "pw")) is True


def test_is_encrypted_false_for_plain():
    from dlw.sdk._crypto import is_encrypted

    assert is_encrypted("plain") is False
    assert is_encrypted("") is False
    assert is_encrypted("Bearer eyJ...") is False


def test_wrong_passphrase_raises():
    from dlw.sdk._crypto import decrypt_token, encrypt_token

    blob = encrypt_token("tok", "correct")
    with pytest.raises(TokenDecryptError):
        decrypt_token(blob, "wrong")


def test_tampered_blob_raises():
    from dlw.sdk._crypto import decrypt_token, encrypt_token

    blob = encrypt_token("tok", "pw")
    # Flip a character in the fernet part (after enc:v1:<salt>:)
    parts = blob.split(":", 3)
    fernet_part = parts[3]
    # Replace the last char with something else
    tampered_fernet = fernet_part[:-1] + ("A" if fernet_part[-1] != "A" else "B")
    tampered = ":".join(parts[:3] + [tampered_fernet])
    with pytest.raises(TokenDecryptError):
        decrypt_token(tampered, "pw")


def test_non_deterministic_encryption():
    from dlw.sdk._crypto import encrypt_token

    a = encrypt_token("tok", "pw")
    b = encrypt_token("tok", "pw")
    assert a != b
