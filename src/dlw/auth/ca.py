"""Self-signed CA + client cert signing + server cert (Phase 2 W3a §3.1)."""
from __future__ import annotations

import datetime as _dt
import ipaddress
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


@dataclass(frozen=True)
class CABundle:
    cert_pem: bytes
    key_pem: bytes
    cert: x509.Certificate
    key: ed25519.Ed25519PrivateKey


def bootstrap_ca(ca_dir: Path) -> CABundle:
    """Idempotent: load existing CA from disk, else generate + persist.
    Files: ca-cert.pem, ca-key.pem (chmod 600). CA validity 10 years."""
    cert_path = ca_dir / "ca-cert.pem"
    key_path = ca_dir / "ca-key.pem"
    if cert_path.exists() and key_path.exists():
        cert_pem = cert_path.read_bytes()
        key_pem = key_path.read_bytes()
        cert = x509.load_pem_x509_certificate(cert_pem)
        key = serialization.load_pem_private_key(key_pem, password=None)
        if not isinstance(key, ed25519.Ed25519PrivateKey):
            raise ValueError("CA key is not Ed25519 (file corrupted)")
        return CABundle(cert_pem=cert_pem, key_pem=key_pem, cert=cert, key=key)

    key = ed25519.Ed25519PrivateKey.generate()
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "dlw-controller-ca"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "modelpull"),
    ])
    now = _dt.datetime.now(_dt.UTC)
    cert = (x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + _dt.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False, content_commitment=False,
                key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=True, crl_sign=True,
                encipher_only=False, decipher_only=False,
            ), critical=True,
        )
        .sign(key, None)
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    ca_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    cert_path.write_bytes(cert_pem)
    cert_path.chmod(0o600)
    key_path.write_bytes(key_pem)
    key_path.chmod(0o600)
    return CABundle(cert_pem=cert_pem, key_pem=key_pem, cert=cert, key=key)


def sign_csr(ca: CABundle, csr_pem: bytes, executor_id: str,
             ttl_hours: int = 24) -> bytes:
    """Sign an executor CSR. CN = executor_id; SAN URI:spiffe://dlw/executor/<id>;
    EKU = CLIENT_AUTH. Raises ValueError on invalid CSR signature."""
    csr = x509.load_pem_x509_csr(csr_pem)
    if not csr.is_signature_valid:
        raise ValueError("CSR signature invalid")
    now = _dt.datetime.now(_dt.UTC)
    cert = (x509.CertificateBuilder()
        .subject_name(x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, executor_id),
        ]))
        .issuer_name(ca.cert.subject)
        .public_key(csr.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + _dt.timedelta(hours=ttl_hours))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([
                x509.UniformResourceIdentifier(f"spiffe://dlw/executor/{executor_id}"),
            ]), critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False,
                key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=False, crl_sign=False,
                encipher_only=False, decipher_only=False,
            ), critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=True,
        )
        .sign(ca.key, None)
    )
    return cert.public_bytes(serialization.Encoding.PEM)


def fingerprint_of(cert_pem: bytes) -> str:
    """SHA256 fingerprint as 'SHA256:<hex>' — stored on executors.cert_fingerprint."""
    cert = x509.load_pem_x509_certificate(cert_pem)
    return f"SHA256:{cert.fingerprint(hashes.SHA256()).hex()}"


def ensure_server_cert(ca: CABundle, ca_dir: Path,
                       hostname: str = "dlw-controller") -> tuple[Path, Path]:
    """Idempotent: load or generate server-cert.pem + server-key.pem (chmod 600).
    CN = hostname. SAN = DNS:localhost, DNS:<hostname>, IP:127.0.0.1, IP:::1.
    TTL 10 years. EKU = SERVER_AUTH. Returns (cert_path, key_path)."""
    cert_path = ca_dir / "server-cert.pem"
    key_path = ca_dir / "server-key.pem"
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path

    key = ed25519.Ed25519PrivateKey.generate()
    now = _dt.datetime.now(_dt.UTC)
    cert = (x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
        .issuer_name(ca.cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + _dt.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.DNSName(hostname),
                x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                x509.IPAddress(ipaddress.ip_address("::1")),
            ]), critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=True,
        )
        .sign(ca.key, None)
    )
    ca_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    cert_path.chmod(0o600)
    key_path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    key_path.chmod(0o600)
    return cert_path, key_path
