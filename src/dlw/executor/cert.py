"""Executor-side cert helpers (Phase 2 W3a §3.10)."""
from __future__ import annotations

from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID


def build_csr(executor_id: str) -> tuple[bytes, bytes]:
    """Generate an Ed25519 keypair + CSR (CN=executor_id).
    Returns (csr_pem, private_key_pem)."""
    key = ed25519.Ed25519PrivateKey.generate()
    csr = (x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, executor_id)]))
        .sign(key, None))
    csr_pem = csr.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return csr_pem, key_pem


def persist(cert_dir: Path, *, cert_pem: bytes, key_pem: bytes,
            ca_chain_pem: bytes, hmac_seed: bytes) -> None:
    """Write client-cert.pem / client-key.pem / ca-chain.pem / hmac-seed
    (chmod 600) into cert_dir (chmod 700)."""
    cert_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name, data in [
        ("client-cert.pem", cert_pem), ("client-key.pem", key_pem),
        ("ca-chain.pem", ca_chain_pem), ("hmac-seed", hmac_seed),
    ]:
        p = cert_dir / name
        p.write_bytes(data)
        p.chmod(0o600)


def load(cert_dir: Path) -> tuple[bytes, bytes, bytes, bytes] | None:
    """Return (cert_pem, key_pem, ca_chain_pem, hmac_seed) or None if absent."""
    paths = [cert_dir / n for n in
             ("client-cert.pem", "client-key.pem", "ca-chain.pem", "hmac-seed")]
    if not all(p.exists() for p in paths):
        return None
    return tuple(p.read_bytes() for p in paths)   # type: ignore[return-value]


def fingerprint(cert_pem: bytes) -> str:
    """SHA256:<hex> — same format as dlw.auth.ca.fingerprint_of."""
    cert = x509.load_pem_x509_certificate(cert_pem)
    return f"SHA256:{cert.fingerprint(hashes.SHA256()).hex()}"
