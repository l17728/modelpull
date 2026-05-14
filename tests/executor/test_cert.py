"""Tests for dlw.executor.cert (Phase 2 W3a §3.10)."""
from __future__ import annotations

from pathlib import Path

from dlw.executor.cert import build_csr, fingerprint, load, persist


def test_build_csr_returns_pem_and_key() -> None:
    csr_pem, key_pem = build_csr("host-1-worker-1")
    assert csr_pem.startswith(b"-----BEGIN CERTIFICATE REQUEST-----")
    assert key_pem.startswith(b"-----BEGIN PRIVATE KEY-----")
    # CSR is parseable + has the right CN
    from cryptography import x509
    csr = x509.load_pem_x509_csr(csr_pem)
    cn = csr.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)[0].value
    assert cn == "host-1-worker-1"
    assert csr.is_signature_valid


def test_persist_and_load_roundtrip(tmp_path) -> None:
    cert_dir = tmp_path / "executor"
    persist(cert_dir, cert_pem=b"CERTDATA", key_pem=b"KEYDATA",
            ca_chain_pem=b"CADATA", hmac_seed=b"\x01" * 32)
    loaded = load(cert_dir)
    assert loaded is not None
    cert_pem, key_pem, ca_chain_pem, hmac_seed = loaded
    assert cert_pem == b"CERTDATA"
    assert key_pem == b"KEYDATA"
    assert ca_chain_pem == b"CADATA"
    assert hmac_seed == b"\x01" * 32


def test_load_returns_none_when_absent(tmp_path) -> None:
    assert load(tmp_path / "nonexistent") is None


def test_fingerprint_matches_controller_format(tmp_path) -> None:
    # Sign a real cert via the controller-side CA so we can compare formats.
    from dlw.auth.ca import bootstrap_ca, sign_csr, fingerprint_of
    ca = bootstrap_ca(tmp_path / "ca")
    csr_pem, _key = build_csr("fp-host-worker-1")
    cert_pem = sign_csr(ca, csr_pem, "fp-host-worker-1")
    assert fingerprint(cert_pem) == fingerprint_of(cert_pem)
    assert fingerprint(cert_pem).startswith("SHA256:")
