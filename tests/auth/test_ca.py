"""Tests for dlw.auth.ca (Phase 2 W3a §3.1)."""
from __future__ import annotations

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ed25519

from dlw.auth.ca import (
    bootstrap_ca,
    ensure_server_cert,
    fingerprint_of,
    sign_csr,
)


def _build_csr(executor_id: str) -> bytes:
    from cryptography.hazmat.primitives import serialization
    from cryptography.x509.oid import NameOID
    key = ed25519.Ed25519PrivateKey.generate()
    csr = (x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, executor_id)]))
        .sign(key, None))
    return csr.public_bytes(serialization.Encoding.PEM)


def test_bootstrap_ca_idempotent(tmp_path) -> None:
    ca1 = bootstrap_ca(tmp_path)
    ca2 = bootstrap_ca(tmp_path)
    assert ca1.cert_pem == ca2.cert_pem
    assert ca1.key_pem == ca2.key_pem


def test_sign_csr_returns_valid_client_cert(tmp_path) -> None:
    ca = bootstrap_ca(tmp_path)
    csr_pem = _build_csr("host-1-worker-1")
    cert_pem = sign_csr(ca, csr_pem, "host-1-worker-1", ttl_hours=24)
    cert = x509.load_pem_x509_certificate(cert_pem)
    cn = cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)[0].value
    assert cn == "host-1-worker-1"
    ca.cert.public_key().verify(cert.signature, cert.tbs_certificate_bytes)
    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH in eku


def test_fingerprint_of_is_deterministic_sha256(tmp_path) -> None:
    ca = bootstrap_ca(tmp_path)
    csr_pem = _build_csr("host-2-worker-1")
    cert_pem = sign_csr(ca, csr_pem, "host-2-worker-1")
    fp1 = fingerprint_of(cert_pem)
    fp2 = fingerprint_of(cert_pem)
    assert fp1 == fp2
    assert fp1.startswith("SHA256:")
    assert len(fp1) == len("SHA256:") + 64


def test_ensure_server_cert_has_required_sans(tmp_path) -> None:
    ca = bootstrap_ca(tmp_path)
    cert_path, key_path = ensure_server_cert(ca, tmp_path, hostname="dlw-controller")
    assert cert_path.exists() and key_path.exists()
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    dns_names = san.get_values_for_type(x509.DNSName)
    ip_addrs = {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
    assert "localhost" in dns_names
    assert "dlw-controller" in dns_names
    assert "127.0.0.1" in ip_addrs
    assert "::1" in ip_addrs
