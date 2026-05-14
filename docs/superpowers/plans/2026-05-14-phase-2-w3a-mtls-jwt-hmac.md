# Phase 2 Week 3a — mTLS + Executor JWT + HMAC Heartbeat Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace executor-side bearer auth with SVID-style mTLS + Ed25519 JWT + HMAC heartbeat (roadmap §2.6 Day 1-3 — SEC-01 + SEC-04). Self-signed CA file-persisted under `${DLW_CA_DIR}`; `POST /register` (CSR signing) replaces W1 `/join`; `POST /{eid}/renew` for cert + JWT lifecycle; in-process nonce store for anti-replay. UI bearer auth retained.

**Architecture:** Controller bootstraps a self-signed CA + Ed25519 JWT signing key + server cert at first startup (file-persisted, chmod 600). New `dlw.auth.{ca,jwt_signing,hmac_nonce}` modules + three chained FastAPI dependencies (`require_executor_mtls` → `require_executor_jwt` → `require_hmac_heartbeat`). `require_executor_epoch` is refactored to chain under the JWT dep and assert the path id matches the mTLS-authenticated identity (confused-deputy guard). Executor generates its own keypair + CSR, registers, persists cert/key/seed locally, and runs a third background loop to renew before expiry. uvicorn terminates TLS via `--ssl-*` flags.

**Tech Stack:** `cryptography>=43,<44` (promoted from transitive to explicit), `pyjwt[crypto]>=2.9,<3.0` (NEW — EdDSA JWT). SQLAlchemy 2.x async + alembic. pytest with ephemeral-CA fixtures; one real-TLS e2e via a uvicorn subprocess. No new CI jobs.

**Scope:** 11 tasks across 4 milestones. Branch `feat/phase-2-w3a-mtls-jwt-hmac` exists with the spec committed (`255a561`). Companion spec: `docs/superpowers/specs/2026-05-14-phase-2-w3a-mtls-jwt-hmac-design.md`.

**Pre-flight:** Phase 2 W2b2 merged into `main` at `ba89a91`. Local PG 18 on `localhost:5433`. `uv` 0.11.9. Existing pytest baseline = 181 passed, 1 deselected. Alembic head `b1d5ea4944ba`.

**Out-of-scope (deferred — see spec §1.2):** HF reverse-proxy (W3b); active/standby + chaos drill (W3c); OIDC / multi-tenant / UI auth (Phase 3); Vault/KMS for keys (Phase 3); CRL / cert-manager (Phase 3+); envelope encryption of `hmac_seed` (Phase 3); PG/Redis nonce store (Phase 3); dual-auth transition window (not needed — hard cutover).

---

## File Structure

After this plan:

```
modelpull/
├── pyproject.toml                                  MODIFY (+cryptography, +pyjwt[crypto])
├── uv.lock                                         MODIFY (uv add regenerates)
├── src/dlw/
│   ├── alembic/versions/<rev>_p2w3a_hmac_seed.py   NEW
│   ├── db/models/executor.py                       MODIFY (+hmac_seed_encrypted column)
│   ├── auth/
│   │   ├── ca.py                                   NEW (CA + sign_csr + fingerprint + ensure_server_cert)
│   │   ├── jwt_signing.py                          NEW (Ed25519 JWT sign/verify)
│   │   ├── hmac_nonce.py                           NEW (NonceStore + compute/verify_hmac)
│   │   ├── executor_mtls.py                        NEW (require_executor_mtls dep)
│   │   ├── executor_jwt_dep.py                     NEW (require_executor_jwt dep)
│   │   ├── hmac_heartbeat_dep.py                   NEW (require_hmac_heartbeat dep)
│   │   ├── executor_epoch.py                       MODIFY (refactor: chain under JWT, assert path id)
│   │   └── bearer.py                               (W1, unchanged — UI only)
│   ├── api/
│   │   ├── executors.py                            MODIFY (/register + /renew NEW; /join DELETED; auth chains)
│   │   └── subtasks.py                             MODIFY (/report auth chain)
│   ├── schemas/executor.py                         MODIFY (+ExecutorRegister/RegistrationResponse/RenewResponse; -ExecutorJoin)
│   ├── services/executor_service.py                MODIFY (join_executor → upsert_executor_with_cert)
│   ├── main.py                                     MODIFY (lifespan bootstrap CA/JWT/nonce/enrollment)
│   ├── config.py                                   MODIFY (+ca_dir, +enrollment_token, +controller_hostname, +tls_trusted_proxy)
│   └── executor/
│       ├── cert.py                                 NEW (build_csr / persist / load / fingerprint)
│       ├── auth_lifecycle.py                       NEW (AuthState / register / renew / load_or_register)
│       ├── client.py                               MODIFY (mTLS + JWT + HMAC; AuthState-driven)
│       ├── runner.py                               MODIFY (load_or_register bootstrap; 3rd bg task)
│       └── config.py                               MODIFY (+enrollment_token, +executor_cert_dir, +executor_ca_bundle)
├── tests/
│   ├── conftest.py                                 MODIFY (+ephemeral_ca, +client_cert_pair, +_signed_heartbeat_headers)
│   ├── auth/
│   │   ├── test_ca.py                              NEW (4 cases)
│   │   ├── test_jwt_signing.py                     NEW (4 cases)
│   │   ├── test_hmac_nonce.py                      NEW (4 cases)
│   │   ├── test_executor_mtls_dep.py               NEW (3 cases)
│   │   ├── test_executor_jwt_dep.py                NEW (2 cases)
│   │   ├── test_hmac_heartbeat_dep.py              NEW (4 cases)
│   │   └── test_executor_epoch.py                  MODIFY (+1 confused-deputy case; migrate /join setups)
│   ├── api/
│   │   ├── test_register_endpoint.py               NEW (3 cases)
│   │   ├── test_renew_endpoint.py                  NEW (2 cases)
│   │   ├── test_executors.py                       MODIFY (joined_executor → registered_executor; HMAC headers)
│   │   └── test_subtasks.py                        MODIFY (fixture migration)
│   ├── e2e/
│   │   ├── test_executor_auth_e2e.py               NEW (1 real-TLS case)
│   │   ├── test_executor_e2e.py                    MODIFY (register flow)
│   │   └── test_happy_path.py                      MODIFY (register flow)
│   └── services/test_executor_service.py           MODIFY (upsert_executor_with_cert rename)
├── tools/lint_invariants.py                        MODIFY (+check_no_bearer_on_executor_routes)
├── api/openapi.yaml                                MODIFY (/register +/renew; -/join; HMAC headers)
└── docs/operator/                                  MODIFY (CA dir, enrollment token, uvicorn --ssl-*, proxy warning)
```

---

## Pre-flight checks

- [ ] On branch `feat/phase-2-w3a-mtls-jwt-hmac`, spec committed (`git log --oneline -1` shows `255a561` or descendant).
- [ ] `main` at `ba89a91` (PR #11 merge): `git log main --oneline -1`.
- [ ] PG running on `localhost:5433` (`pg_isready -h localhost -p 5433`).
- [ ] `dlw` database at alembic head `b1d5ea4944ba` (W2b2): `uv run alembic current`.
- [ ] Existing pytest suite green: `uv run pytest -x` → 181 passed, 1 deselected.

---

## Milestone 1 — Auth substrate

After M1: deps added, `executors.hmac_seed_encrypted` column exists, and `dlw.auth.{ca,jwt_signing,hmac_nonce}` modules work with ~12 unit tests. No endpoint wiring yet.

---

### Task 1: Dependencies + alembic migration + ORM column

**Files:**
- Modify: `pyproject.toml`, `uv.lock`
- Create: `src/dlw/alembic/versions/<rev>_p2w3a_hmac_seed.py`
- Modify: `src/dlw/db/models/executor.py`
- Possibly modify: `tests/db/test_alembic.py`

- [ ] **Step 1: Add the two runtime dependencies**

```
uv add "cryptography>=43,<44" "pyjwt[crypto]>=2.9,<3.0"
```

This updates `pyproject.toml` `dependencies` + regenerates `uv.lock`. Verify:

```
uv run python -c "import cryptography, jwt; print(cryptography.__version__, jwt.__version__)"
```

Expected: prints two version strings (e.g. `43.x.x 2.x.x`).

- [ ] **Step 2: Generate the alembic revision**

```
uv run alembic revision -m "p2w3a hmac_seed"
```

Note the 12-char hex revision id. Open the new file.

- [ ] **Step 3: Verify down_revision**

Confirm:

```python
revision: str = '<new id>'
down_revision: Union[str, None] = 'b1d5ea4944ba'
```

If `down_revision` differs, fix it. (W2b2 `last_paused_at` is the current head.)

- [ ] **Step 4: Implement upgrade/downgrade**

```python
"""p2w3a hmac_seed

Revision ID: <new id>
Revises: b1d5ea4944ba
Create Date: <auto>
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '<new id>'
down_revision: Union[str, None] = 'b1d5ea4944ba'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "executors",
        sa.Column("hmac_seed_encrypted", sa.LargeBinary(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("executors", "hmac_seed_encrypted")
```

Replace both `<new id>` placeholders with the actual revision id.

- [ ] **Step 5: Add the ORM column to `src/dlw/db/models/executor.py`**

Read the file. Find the `Executor` class. After `cert_fingerprint` (W1 column), add:

```python
    # W3a §4: 256-bit HMAC seed for heartbeat anti-replay. "encrypted" in the
    # name is forward-compatible — Phase 2 stores raw bytes; Phase 3 wraps with KMS.
    hmac_seed_encrypted: Mapped[bytes | None] = mapped_column(
        LargeBinary, nullable=True
    )
```

Add `LargeBinary` to the `from sqlalchemy import (...)` import line if absent.

- [ ] **Step 6: Apply migration + verify**

```
uv run alembic upgrade head
psql -h localhost -p 5433 -U postgres -d dlw -c "\d executors" 2>&1 | grep hmac_seed_encrypted
```

Expected: prints a line containing `hmac_seed_encrypted | bytea`.

- [ ] **Step 7: Verify downgrade reverses**

```
uv run alembic downgrade -1
psql -h localhost -p 5433 -U postgres -d dlw -c "\d executors" 2>&1 | grep hmac_seed_encrypted
uv run alembic upgrade head
```

Expected: middle command returns nothing; final re-applies.

- [ ] **Step 8: Update `tests/db/test_alembic.py` if it enumerates columns**

Read `tests/db/test_alembic.py`. If it has an `EXPECTED_*` set listing `executors` columns, add `"hmac_seed_encrypted"`. Otherwise no change.

- [ ] **Step 9: Run full suite**

```
uv run pytest -x
```

Expected: 181 passed, 1 deselected (deps + schema change, no behavior change).

- [ ] **Step 10: Commit**

```bash
git add pyproject.toml uv.lock src/dlw/alembic/versions/ src/dlw/db/models/executor.py tests/db/test_alembic.py
git commit -m "feat(db): p2w3a deps (cryptography+pyjwt) + alembic hmac_seed_encrypted (W3a M1)"
```

---

### Task 2: `dlw.auth.ca` — CA + CSR signing + server cert

**Files:**
- Create: `src/dlw/auth/ca.py`
- Create: `tests/auth/__init__.py` (empty, if `tests/auth/` doesn't already have one)
- Create: `tests/auth/test_ca.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/auth/test_ca.py`:

```python
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
    """Helper: build an Ed25519 CSR for the given executor_id."""
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
    # CN matches
    cn = cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)[0].value
    assert cn == "host-1-worker-1"
    # Signed by the CA
    ca.cert.public_key().verify(cert.signature, cert.tbs_certificate_bytes)
    # EKU = CLIENT_AUTH
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
    assert len(fp1) == len("SHA256:") + 64   # hex sha256


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
```

- [ ] **Step 2: Run — verify ModuleNotFoundError**

```
uv run pytest tests/auth/test_ca.py -v
```

Expected: 4 collection errors, `ModuleNotFoundError: No module named 'dlw.auth.ca'`.

- [ ] **Step 3: Implement `src/dlw/auth/ca.py`**

```python
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
```

> Note: `cert.chmod(0o600)` on Windows is a no-op for the permission bits but does not error — the dev environment is Windows; CI is Linux where it takes effect. This matches how W3a's `.parts/` dirs already behave.

- [ ] **Step 4: Run tests — verify all 4 pass**

```
uv run pytest tests/auth/test_ca.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Run full suite**

```
uv run pytest -x
```

Expected: 185 passed (181 + 4 new), 1 deselected.

- [ ] **Step 6: Commit**

```bash
git add src/dlw/auth/ca.py tests/auth/
git commit -m "feat(auth): ca.py — self-signed CA + CSR signing + server cert (W3a M1)"
```

---

### Task 3: `dlw.auth.jwt_signing` + `dlw.auth.hmac_nonce`

**Files:**
- Create: `src/dlw/auth/jwt_signing.py`
- Create: `src/dlw/auth/hmac_nonce.py`
- Create: `tests/auth/test_jwt_signing.py`
- Create: `tests/auth/test_hmac_nonce.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/auth/test_jwt_signing.py`:

```python
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
                 ttl_seconds=-10)   # already expired
    with pytest.raises(_pyjwt.PyJWTError):
        verify(kp, token)


def test_verify_rejects_wrong_issuer(tmp_path) -> None:
    kp = bootstrap_keypair(tmp_path)
    # Hand-craft a token with a bad issuer using the same key.
    now = int(time.time())
    bad = _pyjwt.encode(
        {"iss": "evil", "sub": "e", "epoch": 1, "scope": "heartbeat",
         "iat": now, "exp": now + 3600},
        kp.priv_pem.decode("utf-8"), algorithm="EdDSA",
    )
    with pytest.raises(_pyjwt.PyJWTError):
        verify(kp, bad)
```

Create `tests/auth/test_hmac_nonce.py`:

```python
"""Tests for dlw.auth.hmac_nonce (Phase 2 W3a §3.3)."""
from __future__ import annotations

import time

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
    fake[0] += 11   # advance past TTL
    assert not store.seen("n1")
```

- [ ] **Step 2: Run — verify ModuleNotFoundError**

```
uv run pytest tests/auth/test_jwt_signing.py tests/auth/test_hmac_nonce.py -v
```

Expected: 8 collection errors, `ModuleNotFoundError`.

- [ ] **Step 3: Implement `src/dlw/auth/jwt_signing.py`**

```python
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
```

- [ ] **Step 4: Implement `src/dlw/auth/hmac_nonce.py`**

```python
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
```

- [ ] **Step 5: Run tests — verify all 8 pass**

```
uv run pytest tests/auth/test_jwt_signing.py tests/auth/test_hmac_nonce.py -v
```

Expected: 8 passed.

- [ ] **Step 6: Run full suite**

```
uv run pytest -x
```

Expected: 193 passed (185 + 8 new), 1 deselected.

- [ ] **Step 7: Commit**

```bash
git add src/dlw/auth/jwt_signing.py src/dlw/auth/hmac_nonce.py tests/auth/test_jwt_signing.py tests/auth/test_hmac_nonce.py
git commit -m "feat(auth): jwt_signing (Ed25519 JWT) + hmac_nonce (NonceStore) (W3a M1)"
```

---

### Milestone 1 verification (self)

- [ ] `cryptography` + `pyjwt` in `pyproject.toml`; `uv sync` clean.
- [ ] alembic head is the new revision; `executors.hmac_seed_encrypted` exists.
- [ ] `ca.py` / `jwt_signing.py` / `hmac_nonce.py` import cleanly; 16 unit tests pass.
- [ ] Full suite at 193.

---

## Milestone 2 — Controller deps + endpoints

After M2: three FastAPI dependencies + `require_executor_epoch` refactor + `/register` + `/renew` endpoints + `main.py` bootstrap. `/join` deleted. ~14 new tests.

---

### Task 4: FastAPI dependencies + `require_executor_epoch` refactor

**Files:**
- Create: `src/dlw/auth/executor_mtls.py`, `src/dlw/auth/executor_jwt_dep.py`, `src/dlw/auth/hmac_heartbeat_dep.py`
- Modify: `src/dlw/auth/executor_epoch.py`
- Modify: `tests/conftest.py` (+ephemeral_ca, +client_cert_pair fixtures)
- Create: `tests/auth/test_executor_mtls_dep.py`, `tests/auth/test_executor_jwt_dep.py`, `tests/auth/test_hmac_heartbeat_dep.py`
- Modify: `tests/auth/test_executor_epoch.py` (+confused-deputy case)

- [ ] **Step 1: Add conftest fixtures**

In `tests/conftest.py`, add at module level (after the existing fixtures):

```python
@pytest.fixture(scope="session")
def ephemeral_ca(tmp_path_factory):
    """One CA + JWT keypair per test session, in a temp dir."""
    from dlw.auth.ca import bootstrap_ca
    from dlw.auth.jwt_signing import bootstrap_keypair
    ca_dir = tmp_path_factory.mktemp("ca")
    ca = bootstrap_ca(ca_dir)
    jwt_kp = bootstrap_keypair(ca_dir)
    return {"ca": ca, "jwt_keypair": jwt_kp, "ca_dir": ca_dir}


@pytest.fixture
def client_cert_pair(ephemeral_ca):
    """Per-test client cert (executor 'test-executor-1') signed by the session CA.
    Returns (cert_pem: bytes, key: Ed25519PrivateKey, executor_id: str)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from dlw.auth.ca import sign_csr
    executor_id = "test-executor-1"
    key = ed25519.Ed25519PrivateKey.generate()
    csr = (x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, executor_id)]))
        .sign(key, None))
    csr_pem = csr.public_bytes(serialization.Encoding.PEM)
    cert_pem = sign_csr(ephemeral_ca["ca"], csr_pem, executor_id, ttl_hours=24)
    return cert_pem, key, executor_id
```

(Don't remove or alter any existing conftest fixture.)

- [ ] **Step 2: Write the failing dependency tests**

Create `tests/auth/test_executor_mtls_dep.py`:

```python
"""Tests for require_executor_mtls (Phase 2 W3a §3.4)."""
from __future__ import annotations

import pytest
from fastapi import FastAPI, Depends
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.auth.ca import fingerprint_of
from dlw.auth.executor_mtls import require_executor_mtls
from dlw.db.base import Base
from dlw.db.models.executor import Executor


@pytest.fixture(scope="module", autouse=True)
async def _create_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _mini_app():
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(ex: Executor = Depends(require_executor_mtls)) -> dict:
        return {"executor_id": ex.id}

    return app


@pytest.mark.slow
async def test_require_executor_mtls_via_trusted_proxy_header(
    engine, client_cert_pair, monkeypatch,
) -> None:
    monkeypatch.setenv("DLW_TLS_TRUSTED_PROXY", "1")
    cert_pem, _key, executor_id = client_cert_pair
    fp = fingerprint_of(cert_pem)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(Executor(id=executor_id, host_id="h", cert_fingerprint=fp,
                       status="healthy", epoch=1))
        await s.commit()

    app = _mini_app()
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        r = await c.get("/whoami", headers={
            "X-Client-Cert-PEM": cert_pem.decode("utf-8").replace("\n", "\\n"),
        })
    assert r.status_code == 200
    assert r.json()["executor_id"] == executor_id


@pytest.mark.slow
async def test_require_executor_mtls_rejects_unknown_fingerprint(
    client_cert_pair, monkeypatch,
) -> None:
    monkeypatch.setenv("DLW_TLS_TRUSTED_PROXY", "1")
    cert_pem, _key, _ = client_cert_pair   # cert NOT inserted into DB
    app = _mini_app()
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        r = await c.get("/whoami", headers={
            "X-Client-Cert-PEM": cert_pem.decode("utf-8").replace("\n", "\\n"),
        })
    assert r.status_code == 401


@pytest.mark.slow
async def test_require_executor_mtls_rejects_header_when_proxy_disabled(
    client_cert_pair, monkeypatch,
) -> None:
    monkeypatch.setenv("DLW_TLS_TRUSTED_PROXY", "0")
    cert_pem, _key, _ = client_cert_pair
    app = _mini_app()
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        r = await c.get("/whoami", headers={
            "X-Client-Cert-PEM": cert_pem.decode("utf-8").replace("\n", "\\n"),
        })
    assert r.status_code == 401
```

Create `tests/auth/test_executor_jwt_dep.py`:

```python
"""Tests for require_executor_jwt (Phase 2 W3a §3.4)."""
from __future__ import annotations

import pytest
from fastapi import FastAPI, Depends
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.auth.ca import fingerprint_of
from dlw.auth.executor_jwt_dep import require_executor_jwt
from dlw.auth.jwt_signing import sign
from dlw.db.base import Base
from dlw.db.models.executor import Executor


@pytest.fixture(scope="module", autouse=True)
async def _create_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _mini_app(jwt_keypair):
    app = FastAPI()
    app.state.jwt_keypair = jwt_keypair

    @app.get("/whoami")
    async def whoami(ex: Executor = Depends(require_executor_jwt)) -> dict:
        return {"executor_id": ex.id}

    return app


@pytest.mark.slow
async def test_require_executor_jwt_accepts_valid_token(
    engine, ephemeral_ca, client_cert_pair, monkeypatch,
) -> None:
    monkeypatch.setenv("DLW_TLS_TRUSTED_PROXY", "1")
    cert_pem, _key, executor_id = client_cert_pair
    fp = fingerprint_of(cert_pem)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(Executor(id=executor_id, host_id="h", cert_fingerprint=fp,
                       status="healthy", epoch=2))
        await s.commit()
    token = sign(ephemeral_ca["jwt_keypair"], executor_id=executor_id,
                 epoch=2, scopes=["heartbeat"])

    app = _mini_app(ephemeral_ca["jwt_keypair"])
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        r = await c.get("/whoami", headers={
            "X-Client-Cert-PEM": cert_pem.decode("utf-8").replace("\n", "\\n"),
            "Authorization": f"Bearer {token}",
        })
    assert r.status_code == 200
    assert r.json()["executor_id"] == executor_id


@pytest.mark.slow
async def test_require_executor_jwt_rejects_sub_mismatch(
    engine, ephemeral_ca, client_cert_pair, monkeypatch,
) -> None:
    monkeypatch.setenv("DLW_TLS_TRUSTED_PROXY", "1")
    cert_pem, _key, executor_id = client_cert_pair
    fp = fingerprint_of(cert_pem)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(Executor(id=executor_id, host_id="h", cert_fingerprint=fp,
                       status="healthy", epoch=2))
        await s.commit()
    # JWT for a DIFFERENT executor
    token = sign(ephemeral_ca["jwt_keypair"], executor_id="other-executor",
                 epoch=2, scopes=["heartbeat"])

    app = _mini_app(ephemeral_ca["jwt_keypair"])
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        r = await c.get("/whoami", headers={
            "X-Client-Cert-PEM": cert_pem.decode("utf-8").replace("\n", "\\n"),
            "Authorization": f"Bearer {token}",
        })
    assert r.status_code == 401
```

Create `tests/auth/test_hmac_heartbeat_dep.py`:

```python
"""Tests for require_hmac_heartbeat (Phase 2 W3a §3.4)."""
from __future__ import annotations

import secrets
import time

import pytest
from fastapi import FastAPI, Depends, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.auth.ca import fingerprint_of
from dlw.auth.hmac_heartbeat_dep import require_hmac_heartbeat
from dlw.auth.hmac_nonce import NonceStore, compute_hmac
from dlw.auth.jwt_signing import sign
from dlw.db.base import Base
from dlw.db.models.executor import Executor


@pytest.fixture(scope="module", autouse=True)
async def _create_tables(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


_HMAC_SEED = b"\x02" * 32


async def _seed_executor(engine, executor_id, fp):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(Executor(id=executor_id, host_id="h", cert_fingerprint=fp,
                       status="healthy", epoch=1,
                       hmac_seed_encrypted=_HMAC_SEED))
        await s.commit()


def _mini_app(jwt_keypair):
    app = FastAPI()
    app.state.jwt_keypair = jwt_keypair
    app.state.nonce_store = NonceStore(maxsize=100, ttl_seconds=300)

    @app.post("/hb")
    async def hb(request: Request,
                 ex: Executor = Depends(require_hmac_heartbeat)) -> dict:
        return {"ok": True, "executor_id": ex.id}

    return app


def _hmac_headers(seed, body: bytes, *, ts: int | None = None, nonce: str | None = None):
    ts = ts if ts is not None else int(time.time())
    nonce = nonce or secrets.token_hex(16)
    sig = compute_hmac(seed, ts=ts, nonce=nonce, body=body)
    return {"X-HMAC-Timestamp": str(ts), "X-HMAC-Nonce": nonce,
            "X-HMAC-Signature": sig}


@pytest.mark.slow
async def test_hmac_heartbeat_accepts_valid_signature(
    engine, ephemeral_ca, client_cert_pair, monkeypatch,
) -> None:
    monkeypatch.setenv("DLW_TLS_TRUSTED_PROXY", "1")
    cert_pem, _key, executor_id = client_cert_pair
    fp = fingerprint_of(cert_pem)
    await _seed_executor(engine, executor_id, fp)
    token = sign(ephemeral_ca["jwt_keypair"], executor_id=executor_id,
                 epoch=1, scopes=["heartbeat"])
    body = b'{"health_score":100}'
    app = _mini_app(ephemeral_ca["jwt_keypair"])
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        r = await c.post("/hb", content=body, headers={
            "X-Client-Cert-PEM": cert_pem.decode("utf-8").replace("\n", "\\n"),
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            **_hmac_headers(_HMAC_SEED, body),
        })
    assert r.status_code == 200


@pytest.mark.slow
async def test_hmac_heartbeat_rejects_clock_skew(
    engine, ephemeral_ca, client_cert_pair, monkeypatch,
) -> None:
    monkeypatch.setenv("DLW_TLS_TRUSTED_PROXY", "1")
    cert_pem, _key, executor_id = client_cert_pair
    fp = fingerprint_of(cert_pem)
    await _seed_executor(engine, executor_id, fp)
    token = sign(ephemeral_ca["jwt_keypair"], executor_id=executor_id,
                 epoch=1, scopes=["heartbeat"])
    body = b'{"health_score":100}'
    app = _mini_app(ephemeral_ca["jwt_keypair"])
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        r = await c.post("/hb", content=body, headers={
            "X-Client-Cert-PEM": cert_pem.decode("utf-8").replace("\n", "\\n"),
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            **_hmac_headers(_HMAC_SEED, body, ts=int(time.time()) - 400),
        })
    assert r.status_code == 401


@pytest.mark.slow
async def test_hmac_heartbeat_rejects_replay(
    engine, ephemeral_ca, client_cert_pair, monkeypatch,
) -> None:
    monkeypatch.setenv("DLW_TLS_TRUSTED_PROXY", "1")
    cert_pem, _key, executor_id = client_cert_pair
    fp = fingerprint_of(cert_pem)
    await _seed_executor(engine, executor_id, fp)
    token = sign(ephemeral_ca["jwt_keypair"], executor_id=executor_id,
                 epoch=1, scopes=["heartbeat"])
    body = b'{"health_score":100}'
    headers_base = {
        "X-Client-Cert-PEM": cert_pem.decode("utf-8").replace("\n", "\\n"),
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    fixed_nonce = "fixed-replay-nonce"
    hmac_h = _hmac_headers(_HMAC_SEED, body, nonce=fixed_nonce)
    app = _mini_app(ephemeral_ca["jwt_keypair"])
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        r1 = await c.post("/hb", content=body, headers={**headers_base, **hmac_h})
        r2 = await c.post("/hb", content=body, headers={**headers_base, **hmac_h})
    assert r1.status_code == 200
    assert r2.status_code == 401   # REPLAY_DETECTED


@pytest.mark.slow
async def test_hmac_heartbeat_rejects_tampered_body(
    engine, ephemeral_ca, client_cert_pair, monkeypatch,
) -> None:
    monkeypatch.setenv("DLW_TLS_TRUSTED_PROXY", "1")
    cert_pem, _key, executor_id = client_cert_pair
    fp = fingerprint_of(cert_pem)
    await _seed_executor(engine, executor_id, fp)
    token = sign(ephemeral_ca["jwt_keypair"], executor_id=executor_id,
                 epoch=1, scopes=["heartbeat"])
    signed_body = b'{"health_score":100}'
    sent_body = b'{"health_score":999}'
    app = _mini_app(ephemeral_ca["jwt_keypair"])
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        r = await c.post("/hb", content=sent_body, headers={
            "X-Client-Cert-PEM": cert_pem.decode("utf-8").replace("\n", "\\n"),
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            **_hmac_headers(_HMAC_SEED, signed_body),   # sig over signed_body
        })
    assert r.status_code == 401   # HMAC_INVALID
```

In `tests/auth/test_executor_epoch.py`, ADD one case (keep existing tests, migrate any `/join`-based setup to a direct DB insert):

```python
@pytest.mark.slow
async def test_require_executor_epoch_rejects_path_id_mismatch(
    engine, ephemeral_ca, client_cert_pair, monkeypatch,
) -> None:
    """mTLS+JWT authenticate executor A, but the URL path says executor B →
    401 EXECUTOR_ID_MISMATCH (confused-deputy guard)."""
    from dlw.auth.ca import fingerprint_of
    from dlw.auth.jwt_signing import sign
    from fastapi import FastAPI, Depends, Path
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from dlw.auth.executor_epoch import require_executor_epoch
    from dlw.db.models.executor import Executor

    monkeypatch.setenv("DLW_TLS_TRUSTED_PROXY", "1")
    cert_pem, _key, executor_id = client_cert_pair   # executor_id == "test-executor-1"
    fp = fingerprint_of(cert_pem)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(Executor(id=executor_id, host_id="h", cert_fingerprint=fp,
                       status="healthy", epoch=1))
        await s.commit()
    token = sign(ephemeral_ca["jwt_keypair"], executor_id=executor_id,
                 epoch=1, scopes=["heartbeat"])

    app = FastAPI()
    app.state.jwt_keypair = ephemeral_ca["jwt_keypair"]

    @app.post("/executors/{executor_id}/x")
    async def x(executor_id: str = Path(...),
                ex: Executor = Depends(require_executor_epoch)) -> dict:
        return {"ok": True}

    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        # path says "other-executor" but cert+JWT are for "test-executor-1"
        r = await c.post("/executors/other-executor/x", headers={
            "X-Client-Cert-PEM": cert_pem.decode("utf-8").replace("\n", "\\n"),
            "Authorization": f"Bearer {token}",
            "X-Executor-Epoch": "1",
        })
    assert r.status_code == 401
```

- [ ] **Step 3: Run — verify the dep tests fail with ModuleNotFoundError**

```
uv run pytest tests/auth/test_executor_mtls_dep.py tests/auth/test_executor_jwt_dep.py tests/auth/test_hmac_heartbeat_dep.py -v
```

Expected: collection errors for the three missing modules.

- [ ] **Step 4: Implement `src/dlw/auth/executor_mtls.py`**

```python
"""mTLS peer-cert dependency (Phase 2 W3a §3.4)."""
from __future__ import annotations

import os

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.api.tasks import _session
from dlw.auth.ca import fingerprint_of
from dlw.db.models.executor import Executor


def _extract_peer_cert(request: Request) -> bytes | None:
    """Two paths: (a) direct uvicorn TLS — peercert in scope; (b) trusted-proxy
    forwarded header — only honored when DLW_TLS_TRUSTED_PROXY=1."""
    transport = request.scope.get("transport")
    if transport is not None:
        peercert = transport.get_extra_info("peercert") if hasattr(
            transport, "get_extra_info") else None
        if peercert:
            # uvicorn provides the DER-encoded peer cert
            try:
                cert = x509.load_der_x509_certificate(peercert)
                return cert.public_bytes(serialization.Encoding.PEM)
            except Exception:
                pass
    if os.environ.get("DLW_TLS_TRUSTED_PROXY") == "1":
        header = request.headers.get("X-Client-Cert-PEM")
        if header:
            return header.replace("\\n", "\n").encode("utf-8")
    return None


async def require_executor_mtls(
    request: Request,
    session: AsyncSession = Depends(_session),
) -> Executor:
    """Validate mTLS peer cert + look up executor by fingerprint."""
    cert_pem = _extract_peer_cert(request)
    if cert_pem is None:
        raise HTTPException(401, detail="missing or invalid mTLS peer cert")
    try:
        fp = fingerprint_of(cert_pem)
    except Exception as e:
        raise HTTPException(401, detail=f"invalid client cert: {e}") from e
    ex = (await session.execute(
        select(Executor).where(Executor.cert_fingerprint == fp)
    )).scalar_one_or_none()
    if ex is None:
        raise HTTPException(401, detail="cert fingerprint not registered")
    return ex
```

- [ ] **Step 5: Implement `src/dlw/auth/executor_jwt_dep.py`**

```python
"""Executor JWT dependency (Phase 2 W3a §3.4)."""
from __future__ import annotations

import jwt as _pyjwt
from fastapi import Depends, Header, HTTPException, Request

from dlw.auth.executor_mtls import require_executor_mtls
from dlw.auth.jwt_signing import verify
from dlw.db.models.executor import Executor


async def require_executor_jwt(
    request: Request,
    authorization: str | None = Header(default=None),
    ex: Executor = Depends(require_executor_mtls),
) -> Executor:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, detail="missing executor JWT")
    token = authorization.split(" ", 1)[1]
    try:
        claims = verify(request.app.state.jwt_keypair, token)
    except _pyjwt.PyJWTError as e:
        raise HTTPException(401, detail=f"invalid JWT: {e}") from e
    if claims["sub"] != ex.id:
        raise HTTPException(401, detail="JWT sub mismatch")
    return ex
```

- [ ] **Step 6: Implement `src/dlw/auth/hmac_heartbeat_dep.py`**

```python
"""HMAC heartbeat dependency (Phase 2 W3a §3.4)."""
from __future__ import annotations

import time

from fastapi import Depends, Header, HTTPException, Request

from dlw.auth.executor_jwt_dep import require_executor_jwt
from dlw.auth.hmac_nonce import verify_hmac
from dlw.db.models.executor import Executor

_TIMESTAMP_SKEW_SECONDS = 300


async def require_hmac_heartbeat(
    request: Request,
    x_hmac_timestamp: int = Header(..., alias="X-HMAC-Timestamp"),
    x_hmac_nonce: str = Header(..., alias="X-HMAC-Nonce"),
    x_hmac_signature: str = Header(..., alias="X-HMAC-Signature"),
    ex: Executor = Depends(require_executor_jwt),
) -> Executor:
    now = int(time.time())
    if abs(now - x_hmac_timestamp) > _TIMESTAMP_SKEW_SECONDS:
        raise HTTPException(401, detail="CLOCK_SKEW")
    store = request.app.state.nonce_store
    if store.seen(x_hmac_nonce):
        raise HTTPException(401, detail="REPLAY_DETECTED")
    if ex.hmac_seed_encrypted is None:
        raise HTTPException(401, detail="HMAC_SEED_MISSING — re-register")
    hmac_seed = bytes(ex.hmac_seed_encrypted)
    body = await request.body()
    if not verify_hmac(hmac_seed, ts=x_hmac_timestamp, nonce=x_hmac_nonce,
                       body=body, signature_hex=x_hmac_signature):
        raise HTTPException(401, detail="HMAC_INVALID")
    store.add(x_hmac_nonce)
    return ex
```

- [ ] **Step 7: Refactor `src/dlw/auth/executor_epoch.py`**

Replace the whole file with:

```python
"""require_executor_epoch — W1 fence-token dep, refactored for W3a §3.4.

Under W3a it chains under require_executor_jwt: the Executor row is already
loaded + authenticated via the mTLS cert fingerprint. This dep adds two
checks on top:
  1. the path executor_id MUST equal the mTLS-authenticated identity
     (confused-deputy guard — W3a closes the gap where a valid cert for
     executor A could be used against /executors/B/...);
  2. X-Executor-Epoch MUST match the authenticated row's epoch (W1 fence).
"""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Path

from dlw.auth.executor_jwt_dep import require_executor_jwt
from dlw.db.models.executor import Executor


async def require_executor_epoch(
    executor_id: str = Path(..., description="Executor id from URL path"),
    x_executor_epoch: int | None = Header(default=None, alias="X-Executor-Epoch"),
    ex: Executor = Depends(require_executor_jwt),
) -> Executor:
    """Return the mTLS+JWT-authenticated Executor row if the path id matches
    and the epoch header matches; else 401."""
    if executor_id != ex.id:
        raise HTTPException(
            status_code=401,
            detail={"code": "EXECUTOR_ID_MISMATCH",
                    "path": executor_id, "authenticated": ex.id},
        )
    if x_executor_epoch is None:
        raise HTTPException(status_code=401, detail="missing X-Executor-Epoch header")
    if ex.epoch != x_executor_epoch:
        raise HTTPException(
            status_code=401,
            detail={"code": "EPOCH_MISMATCH", "expected": ex.epoch,
                    "got": x_executor_epoch},
        )
    return ex
```

> The old W1 version did its own `session.get(Executor, executor_id)`; the W3a version receives the already-loaded + authenticated row from `require_executor_jwt`. The `from dlw.api.tasks import _session` import is removed (no longer does its own lookup).

- [ ] **Step 8: Run the dep tests — verify all pass**

```
uv run pytest tests/auth/ -v
```

Expected: all `tests/auth/` pass — the new dep tests + the existing W1 epoch tests (migrated) + the new confused-deputy case. If a W1 `test_executor_epoch.py` case constructed an executor via `/join`, replace that setup with a direct DB insert + a signed JWT (mirror the new case's setup).

- [ ] **Step 9: Run full suite**

```
uv run pytest -x
```

Expected: 193 (M1) + ~13 new auth dep tests = ~206; existing W1 epoch tests may have been rewritten but count stays roughly same. Some W1 API tests (`test_executors.py`, `test_subtasks.py`) will now FAIL because their endpoints still use the old `require_executor_epoch` signature indirectly — that's expected; Task 6 migrates them. **For this task, run `uv run pytest tests/auth/ -x` and confirm green; the full-suite breakage in `tests/api/` is addressed in Task 6.** Note the count of failures so Task 6 can confirm they all clear.

- [ ] **Step 10: Commit**

```bash
git add src/dlw/auth/executor_mtls.py src/dlw/auth/executor_jwt_dep.py src/dlw/auth/hmac_heartbeat_dep.py src/dlw/auth/executor_epoch.py tests/conftest.py tests/auth/
git commit -m "feat(auth): mTLS + JWT + HMAC FastAPI deps; executor_epoch confused-deputy guard (W3a M2)"
```

---

### Task 5: `/register` + `/renew` endpoints + `main.py` bootstrap + service rename

**Files:**
- Modify: `src/dlw/schemas/executor.py`
- Modify: `src/dlw/services/executor_service.py`
- Modify: `src/dlw/api/executors.py`
- Modify: `src/dlw/main.py`
- Modify: `src/dlw/config.py`
- Create: `tests/api/test_register_endpoint.py`, `tests/api/test_renew_endpoint.py`

- [ ] **Step 1: Add schemas to `src/dlw/schemas/executor.py`**

Read the file. Add (and DELETE the W1 `ExecutorJoin` schema if present):

```python
class ExecutorRegister(BaseModel):
    host_id: str
    executor_id_proposal: str
    capabilities: dict[str, Any] = {}
    client_csr_pem: str


class RegistrationResponse(BaseModel):
    executor_id: str
    epoch: int
    client_cert_pem: str
    ca_chain: list[str]
    executor_jwt: str
    hmac_seed_hex: str
    cert_renew_in_seconds: int
    jwt_renew_in_seconds: int


class RenewRequest(BaseModel):
    # Optional: the executor includes a fresh CSR only when its cert is near
    # expiry. When None, /renew refreshes the JWT only.
    client_csr_pem: str | None = None


class RenewResponse(BaseModel):
    executor_jwt: str
    jwt_renew_in_seconds: int
    client_cert_pem: str | None = None
    cert_renew_in_seconds: int | None = None
```

Add `from typing import Any` to imports if absent.

- [ ] **Step 2: Rename + extend `join_executor` in `src/dlw/services/executor_service.py`**

Read the file. The W1 `join_executor` does a `pg_insert ... ON CONFLICT DO UPDATE` epoch-bump. Rename it to `upsert_executor_with_cert` and add `cert_fingerprint` + `hmac_seed` params:

```python
async def upsert_executor_with_cert(
    session: AsyncSession,
    *,
    executor_id: str,
    host_id: str,
    capabilities: dict[str, Any],
    cert_fingerprint: str,
    hmac_seed: bytes,
) -> Executor:
    """W3a §3.8: INSERT-or-bump executor row, writing cert_fingerprint +
    hmac_seed_encrypted. Same atomic epoch semantics as W1 join_executor:
    epoch=1 on insert, +1 on conflict. Caller commits."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    stmt = (
        pg_insert(Executor)
        .values(
            id=executor_id, host_id=host_id, capabilities=capabilities,
            cert_fingerprint=cert_fingerprint, hmac_seed_encrypted=hmac_seed,
            status="joining", epoch=1,
        )
        .on_conflict_do_update(
            index_elements=["id"],
            set_={
                "host_id": host_id,
                "capabilities": capabilities,
                "cert_fingerprint": cert_fingerprint,
                "hmac_seed_encrypted": hmac_seed,
                "status": "joining",
                "epoch": Executor.epoch + 1,
            },
        )
        .returning(Executor)
    )
    row = (await session.execute(stmt)).scalar_one()
    return row
```

(Match the exact W1 `join_executor` structure — read it first. The W1 version may set `status="joining"` or `status="healthy"`; preserve whatever W1 did. The W3a additions are the two new columns. If W1's `join_executor` had different param names, keep its body shape and only add the cert + seed handling.)

Keep a thin `join_executor` alias removed entirely — grep for callers and migrate them all to `upsert_executor_with_cert` (Task 6 + the test migration handle the test callers).

- [ ] **Step 3: Rewrite the executor endpoints in `src/dlw/api/executors.py`**

Read the file. DELETE the `POST /join` endpoint. Add imports:

```python
import secrets
from cryptography import x509
from dlw.auth.ca import fingerprint_of, sign_csr
from dlw.auth import jwt_signing
from dlw.auth.executor_jwt_dep import require_executor_jwt
from dlw.schemas.executor import (
    ExecutorRegister, RegistrationResponse, RenewRequest, RenewResponse,
)
from dlw.services.executor_service import upsert_executor_with_cert
```

Add `/register`:

```python
@router.post("/register", status_code=status.HTTP_201_CREATED)
async def post_register(
    body: ExecutorRegister,
    request: Request,
    x_enrollment_token: str = Header(..., alias="X-Enrollment-Token"),
    session: AsyncSession = Depends(_session),
) -> RegistrationResponse:
    """W3a §3.5: enrollment-token auth; signs CSR; returns cert + JWT + hmac_seed."""
    expected = request.app.state.enrollment_token
    if not secrets.compare_digest(x_enrollment_token, expected):
        raise HTTPException(401, detail="invalid enrollment token")
    try:
        cert_pem = sign_csr(
            request.app.state.ca,
            body.client_csr_pem.encode("utf-8"),
            executor_id=body.executor_id_proposal,
            ttl_hours=24,
        )
    except ValueError as e:
        raise HTTPException(422, detail=f"invalid CSR: {e}") from e
    fp = fingerprint_of(cert_pem)
    hmac_seed = secrets.token_bytes(32)
    ex = await upsert_executor_with_cert(
        session, executor_id=body.executor_id_proposal,
        host_id=body.host_id, capabilities=body.capabilities,
        cert_fingerprint=fp, hmac_seed=hmac_seed,
    )
    token = jwt_signing.sign(
        request.app.state.jwt_keypair,
        executor_id=ex.id, epoch=ex.epoch,
        scopes=["heartbeat", "poll", "report"],
    )
    await session.commit()
    return RegistrationResponse(
        executor_id=ex.id, epoch=ex.epoch,
        client_cert_pem=cert_pem.decode("utf-8"),
        ca_chain=[request.app.state.ca.cert_pem.decode("utf-8")],
        executor_jwt=token,
        hmac_seed_hex=hmac_seed.hex(),
        cert_renew_in_seconds=86100,
        jwt_renew_in_seconds=3300,
    )
```

Add `/renew` — the executor sends an optional fresh CSR in the body (the controller cannot re-sign from a bare public key; a CSR is self-signed by the executor's private key, which the controller never holds). When `client_csr_pem` is present, sign a new cert; otherwise refresh the JWT only:

```python
@router.post("/{executor_id}/renew")
async def post_renew(
    executor_id: str,
    body: RenewRequest,
    request: Request,
    ex: Executor = Depends(require_executor_jwt),
    session: AsyncSession = Depends(_session),
) -> RenewResponse:
    """W3a §3.5: always refresh the JWT; sign a new cert iff the request
    carries a fresh CSR (the executor includes one when its cert is near
    expiry — see Task 9's renew loop)."""
    if executor_id != ex.id:
        raise HTTPException(401, detail="path executor_id mismatch")
    new_jwt = jwt_signing.sign(
        request.app.state.jwt_keypair,
        executor_id=ex.id, epoch=ex.epoch,
        scopes=["heartbeat", "poll", "report"],
    )
    new_cert_pem: str | None = None
    new_cert_renew_in: int | None = None
    if body.client_csr_pem:
        try:
            new_cert_bytes = sign_csr(
                request.app.state.ca,
                body.client_csr_pem.encode("utf-8"),
                executor_id=ex.id, ttl_hours=24,
            )
        except ValueError as e:
            raise HTTPException(422, detail=f"invalid CSR: {e}") from e
        new_cert_pem = new_cert_bytes.decode("utf-8")
        ex.cert_fingerprint = fingerprint_of(new_cert_bytes)
        new_cert_renew_in = 86100
    await session.commit()
    return RenewResponse(
        executor_jwt=new_jwt, jwt_renew_in_seconds=3300,
        client_cert_pem=new_cert_pem,
        cert_renew_in_seconds=new_cert_renew_in,
    )
```

Add `RenewRequest` to the imports from `dlw.schemas.executor` alongside `ExecutorRegister` / `RegistrationResponse` / `RenewResponse`.

Migrate `/heartbeat` and `/poll` dependency chains in this same file:
- `/heartbeat`: remove `Depends(require_bearer)`; the handler param `executor: Executor = Depends(require_executor_epoch)` stays, ADD `_hmac: Executor = Depends(require_hmac_heartbeat)`.
- `/poll`: remove `Depends(require_bearer)`; `executor: Executor = Depends(require_executor_epoch)` stays (it now transitively requires mTLS+JWT).

(The W1 `/heartbeat` and `/poll` already use `Depends(require_executor_epoch)` — after Task 4's refactor that dep already pulls mTLS+JWT. So the only change here is dropping `require_bearer` and adding the HMAC dep to `/heartbeat`.)

- [ ] **Step 4: Bootstrap in `src/dlw/main.py`**

Read `main.py`. In `lifespan`, BEFORE the W1 `run_recovery_routine` call, add:

```python
    from pathlib import Path
    from dlw.auth.ca import bootstrap_ca, ensure_server_cert
    from dlw.auth.jwt_signing import bootstrap_keypair
    from dlw.auth.hmac_nonce import NonceStore
    import secrets as _secrets
    from dlw.config import get_settings as _gs
    _settings = _gs()
    ca_dir = Path(_settings.ca_dir)
    ca_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    _ca = bootstrap_ca(ca_dir)
    ensure_server_cert(_ca, ca_dir, hostname=_settings.controller_hostname)
    _jwt_kp = bootstrap_keypair(ca_dir)
    # Enrollment token: env override > file > generate-and-persist.
    if _settings.enrollment_token:
        _enroll = _settings.enrollment_token
    else:
        _tok_path = ca_dir / "enrollment.token"
        if _tok_path.exists():
            _enroll = _tok_path.read_text().strip()
        else:
            _enroll = _secrets.token_hex(32)
            _tok_path.write_text(_enroll)
            _tok_path.chmod(0o600)
            logger.info("generated enrollment token (copy to executors): %s", _enroll)
    app.state.ca = _ca
    app.state.jwt_keypair = _jwt_kp
    app.state.nonce_store = NonceStore(maxsize=10_000, ttl_seconds=300)
    app.state.enrollment_token = _enroll
```

`app` is available in `lifespan(app)` — confirm the signature. Place the block so `app.state.*` is set before the app serves traffic.

- [ ] **Step 5: Add config fields to `src/dlw/config.py`**

In `Settings`, add:

```python
    # Phase 2 W3a — mTLS + JWT + HMAC
    ca_dir: str = Field(default="./.ca")
    enrollment_token: str = Field(default="")
    controller_hostname: str = Field(default="dlw-controller")
    tls_trusted_proxy: bool = Field(default=False)
```

Env vars: `DLW_CA_DIR`, `DLW_ENROLLMENT_TOKEN`, `DLW_CONTROLLER_HOSTNAME`, `DLW_TLS_TRUSTED_PROXY`.

- [ ] **Step 6: Write the endpoint tests**

Create `tests/api/test_register_endpoint.py` — 3 cases: `test_register_returns_cert_jwt_and_hmac_seed`, `test_register_rejects_invalid_enrollment_token`, `test_register_idempotent_on_reregister`. Use the `ephemeral_ca` fixture; the test app's `app.state.ca / jwt_keypair / enrollment_token` are set from it. Build a CSR with `_build_csr` (copy the helper from `test_ca.py` or hoist it into conftest). Assert: 201 + all 4 response fields populated; 401 on wrong token; epoch bumps on re-register.

Create `tests/api/test_renew_endpoint.py` — 2 cases: `test_renew_returns_new_jwt_only_when_cert_fresh` (register, then renew with no CSR → `client_cert_pem` is None), `test_renew_returns_new_cert_when_csr_provided` (renew with a fresh CSR → returns a new cert). Both use the `DLW_TLS_TRUSTED_PROXY=1` header bypass for the mTLS dep.

Write the full test bodies following the `tests/api/test_cancel_endpoint.py` pattern (httpx `AsyncClient` + `ASGITransport` + `_bootstrap` fixture that creates tables + seeds tenant/project/user/storage). The app fixture must set `app.state.ca / jwt_keypair / nonce_store / enrollment_token` — either let the real `lifespan` run (it bootstraps into a tmp `DLW_CA_DIR`) or set them manually on `create_app()`'s result. Prefer letting `lifespan` run with `DLW_CA_DIR` monkeypatched to a tmp dir.

- [ ] **Step 7: Run the new endpoint tests**

```
uv run pytest tests/api/test_register_endpoint.py tests/api/test_renew_endpoint.py -v
```

Expected: 5 passed.

- [ ] **Step 8: Commit** (the api/ suite is still broken — Task 6 fixes it)

```bash
git add src/dlw/schemas/executor.py src/dlw/services/executor_service.py src/dlw/api/executors.py src/dlw/main.py src/dlw/config.py tests/api/test_register_endpoint.py tests/api/test_renew_endpoint.py
git commit -m "feat(api): /register + /renew endpoints + main bootstrap; delete /join (W3a M2)"
```

---

### Milestone 2 verification (self)

- [ ] `tests/auth/` fully green.
- [ ] `tests/api/test_register_endpoint.py` + `test_renew_endpoint.py` green.
- [ ] `/join` is deleted from `executors.py` and `ExecutorJoin` from `schemas/executor.py`.
- [ ] `main.py lifespan` bootstraps CA + JWT key + nonce store + enrollment token onto `app.state`.
- [ ] `tests/api/test_executors.py` + `test_subtasks.py` are EXPECTED to be red here — Task 6 migrates them.

---

## Milestone 3 — Endpoint auth migration + e2e

After M3: all W1 executor/subtask test setups migrated to `/register`; the real-TLS e2e passes; full suite green.

---

### Task 6: Migrate W1 executor + subtask test setups

**Files:**
- Modify: `src/dlw/api/subtasks.py`
- Modify: `tests/api/test_executors.py`, `tests/api/test_subtasks.py`
- Modify: `tests/e2e/test_executor_e2e.py`, `tests/e2e/test_happy_path.py`
- Modify: `tests/services/test_executor_service.py`
- Modify: `tests/conftest.py` (+`_signed_heartbeat_headers` + `registered_executor` helper)

- [ ] **Step 1: Migrate `subtasks.py` `/report` auth chain**

Read `src/dlw/api/subtasks.py`. The `/report` endpoint currently uses `Depends(require_bearer)` (and possibly `require_executor_epoch`). Remove `require_bearer`; ensure the chain is `Depends(require_executor_jwt)` + `Depends(require_executor_epoch)` (the latter transitively pulls mTLS+JWT). Body shape unchanged.

- [ ] **Step 2: Add conftest helpers**

In `tests/conftest.py`, add a `registered_executor` async helper + `_signed_heartbeat_headers`:

```python
async def register_test_executor(client, *, ca, jwt_keypair, enrollment_token,
                                  executor_id="test-host-worker-1",
                                  host_id="test-host"):
    """Build a CSR, POST /register, return a dict with cert_pem, key, jwt,
    hmac_seed, epoch. For use in API tests that need an authenticated executor."""
    # build CSR
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    key = ed25519.Ed25519PrivateKey.generate()
    csr = (x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, executor_id)]))
        .sign(key, None))
    csr_pem = csr.public_bytes(serialization.Encoding.PEM).decode("utf-8")
    r = await client.post("/api/v1/executors/register", json={
        "host_id": host_id, "executor_id_proposal": executor_id,
        "capabilities": {}, "client_csr_pem": csr_pem,
    }, headers={"X-Enrollment-Token": enrollment_token})
    assert r.status_code in (200, 201), r.text
    body = r.json()
    return {
        "executor_id": body["executor_id"], "epoch": body["epoch"],
        "cert_pem": body["client_cert_pem"], "jwt": body["executor_jwt"],
        "hmac_seed": bytes.fromhex(body["hmac_seed_hex"]),
        "ca_chain": body["ca_chain"],
    }


def signed_heartbeat_headers(reg: dict, body: bytes) -> dict[str, str]:
    """Compute the mTLS-bypass + JWT + HMAC + epoch headers for a heartbeat
    body, given the dict returned by register_test_executor."""
    import secrets as _s, time as _t
    from dlw.auth.hmac_nonce import compute_hmac
    ts = int(_t.time())
    nonce = _s.token_hex(16)
    sig = compute_hmac(reg["hmac_seed"], ts=ts, nonce=nonce, body=body)
    return {
        "X-Client-Cert-PEM": reg["cert_pem"].replace("\n", "\\n"),
        "Authorization": f"Bearer {reg['jwt']}",
        "X-Executor-Epoch": str(reg["epoch"]),
        "X-HMAC-Timestamp": str(ts),
        "X-HMAC-Nonce": nonce,
        "X-HMAC-Signature": sig,
        "Content-Type": "application/json",
    }
```

(These are plain helper functions, not fixtures — import them in the test files that need them.)

- [ ] **Step 3: Migrate `tests/api/test_executors.py`**

Read the file. The `joined_executor` fixture calls `POST /api/v1/executors/join`. Replace it with a `registered_executor` fixture that:
1. Ensures `DLW_TLS_TRUSTED_PROXY=1` (via the existing `_set_token`-style env fixture or a new one).
2. Lets the app's `lifespan` bootstrap the CA (monkeypatch `DLW_CA_DIR` to a tmp dir, OR set `app.state` manually).
3. Calls `register_test_executor(...)` and returns the reg dict.

Every test that does `POST /heartbeat` / `/poll` now needs the mTLS+JWT(+HMAC) headers — use `signed_heartbeat_headers(reg, body)` for heartbeat and a plain JWT+cert+epoch header set for poll. The W1 `_TOKEN` shared-bearer fixture is removed for executor endpoints (UI endpoints keep it, but `test_executors.py` only hits executor endpoints).

Migrate the unauthenticated-rejection test: it should now assert that a request with NO cert header gets 401.

- [ ] **Step 4: Migrate `tests/api/test_subtasks.py`**

Same pattern: the `/report` calls need cert + JWT + epoch headers (no HMAC — report isn't HMAC-protected). Use `register_test_executor` + a `report_headers(reg)` helper (cert + JWT + epoch only).

- [ ] **Step 5: Migrate `tests/e2e/test_executor_e2e.py` + `tests/e2e/test_happy_path.py`**

These run a fuller flow. The W1 `/join` call at the start becomes `/register`. The mocked controller responses / runner wiring change to carry the new auth. Read each file; the changes are mechanical (swap `/join` → `/register`, attach the new headers). If a test mocks `ControllerClient` directly, update the mock to the W3a client surface (Task 9 defines it — for now, mock at the same boundary).

- [ ] **Step 6: Migrate `tests/services/test_executor_service.py`**

`join_executor` is renamed to `upsert_executor_with_cert`. Update the import + call sites. The W1 INSERT-or-bump cases still apply — add two assertions per case: `ex.cert_fingerprint` is set, `ex.hmac_seed_encrypted` is set. Pass synthetic `cert_fingerprint="SHA256:..."` + `hmac_seed=b"\x00"*32` in the test calls.

- [ ] **Step 7: Run the full suite**

```
uv run pytest -x
```

Expected: green. Count ≈ 193 (M1) + 13 (Task 4 deps) + 5 (Task 5 endpoints) + ~1 (Task 4 confused-deputy) = ~212, minus/plus the W1 test rewrites (same count, different bodies). The exact number depends on how the W1 fixtures were structured — the key check is **0 failures**.

If failures remain, they're almost certainly missed header migrations in `test_executors.py` / `test_subtasks.py` — fix them.

- [ ] **Step 8: Commit**

```bash
git add src/dlw/api/subtasks.py tests/conftest.py tests/api/test_executors.py tests/api/test_subtasks.py tests/e2e/test_executor_e2e.py tests/e2e/test_happy_path.py tests/services/test_executor_service.py
git commit -m "feat(api): migrate executor + subtask endpoints + test setups to mTLS+JWT (W3a M3)"
```

---

### Task 7: Real-TLS e2e test

**Files:**
- Create: `tests/e2e/test_executor_auth_e2e.py`

- [ ] **Step 1: Write the e2e test**

Create `tests/e2e/test_executor_auth_e2e.py`. This test spawns uvicorn in a subprocess with real `--ssl-*` flags and connects with an httpx client doing real mTLS.

```python
"""Real-TLS e2e: register → heartbeat full flow (Phase 2 W3a §7.2)."""
from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

from dlw.auth.ca import bootstrap_ca, ensure_server_cert


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.slow
async def test_register_then_heartbeat_full_flow(tmp_path, test_db_name) -> None:
    """Spawn uvicorn with real TLS; register an executor; send an HMAC-signed
    heartbeat over mTLS. Verifies the uvicorn wiring + peer-cert extraction."""
    ca_dir = tmp_path / "ca"
    ca_dir.mkdir()
    ca = bootstrap_ca(ca_dir)
    server_cert, server_key = ensure_server_cert(ca, ca_dir, hostname="localhost")
    port = _free_port()
    enrollment_token = "e2e-enrollment-token"

    env = {
        **os.environ,
        "DLW_CA_DIR": str(ca_dir),
        "DLW_ENROLLMENT_TOKEN": enrollment_token,
        "DLW_CONTROLLER_HOSTNAME": "localhost",
        "DLW_DB_NAME": test_db_name,
        # ... DB host/port/user from the conftest env pattern ...
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "dlw.main:app",
         "--host", "127.0.0.1", "--port", str(port),
         "--ssl-keyfile", str(server_key),
         "--ssl-certfile", str(server_cert),
         "--ssl-ca-certs", str(ca_dir / "ca-cert.pem"),
         "--ssl-cert-reqs", "2"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        # Wait for the server to be ready (poll /healthz over TLS).
        base = f"https://localhost:{port}"
        for _ in range(50):
            try:
                async with httpx.AsyncClient(verify=str(ca_dir / "ca-cert.pem")) as c:
                    # /healthz may not require mTLS; if it does, skip this probe
                    r = await c.get(f"{base}/healthz", timeout=1.0)
                    if r.status_code < 500:
                        break
            except Exception:
                await asyncio.sleep(0.2)
        else:
            out = proc.stdout.read().decode() if proc.stdout else ""
            pytest.fail(f"uvicorn did not start: {out}")

        # Build a CSR + register (no mTLS for /register — enrollment token).
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        key = ed25519.Ed25519PrivateKey.generate()
        csr = (x509.CertificateSigningRequestBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "e2e-worker-1")]))
            .sign(key, None))
        csr_pem = csr.public_bytes(serialization.Encoding.PEM).decode()

        async with httpx.AsyncClient(verify=str(ca_dir / "ca-cert.pem")) as c:
            reg = await c.post(f"{base}/api/v1/executors/register", json={
                "host_id": "e2e-host", "executor_id_proposal": "e2e-worker-1",
                "capabilities": {}, "client_csr_pem": csr_pem,
            }, headers={"X-Enrollment-Token": enrollment_token})
        assert reg.status_code == 201, reg.text
        body = reg.json()

        # Persist the issued client cert + key for the mTLS heartbeat call.
        client_cert_path = tmp_path / "client-cert.pem"
        client_key_path = tmp_path / "client-key.pem"
        client_cert_path.write_text(body["client_cert_pem"])
        client_key_path.write_bytes(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ))

        # Heartbeat over real mTLS + JWT + HMAC.
        import secrets, json
        from dlw.auth.hmac_nonce import compute_hmac
        hb_body = json.dumps({"health_score": 100, "parts_dir_bytes": 0}).encode()
        ts = int(time.time())
        nonce = secrets.token_hex(16)
        sig = compute_hmac(bytes.fromhex(body["hmac_seed_hex"]),
                           ts=ts, nonce=nonce, body=hb_body)
        async with httpx.AsyncClient(
            verify=str(ca_dir / "ca-cert.pem"),
            cert=(str(client_cert_path), str(client_key_path)),
        ) as c:
            hb = await c.post(
                f"{base}/api/v1/executors/e2e-worker-1/heartbeat",
                content=hb_body,
                headers={
                    "Authorization": f"Bearer {body['executor_jwt']}",
                    "X-Executor-Epoch": str(body["epoch"]),
                    "X-HMAC-Timestamp": str(ts),
                    "X-HMAC-Nonce": nonce,
                    "X-HMAC-Signature": sig,
                    "Content-Type": "application/json",
                },
            )
        assert hb.status_code == 200, hb.text
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
```

> The test needs the DB env vars matching the conftest pattern — read `tests/conftest.py`'s `_pg_env()` and replicate the host/port/user/password into the subprocess `env`. The subprocess controller bootstraps its OWN `DLW_CA_DIR` but the test pre-creates the CA + server cert so the `verify=` path is known. Since `bootstrap_ca` is idempotent, the subprocess loads the same CA the test created.

- [ ] **Step 2: Run the e2e test**

```
uv run pytest tests/e2e/test_executor_auth_e2e.py -v
```

Expected: 1 passed. If uvicorn fails to start, the test fails fast with the captured stdout. Common issues: missing `--ssl-*` file paths, the controller's own `lifespan` `bootstrap_ca` colliding with the pre-created files (it shouldn't — `bootstrap_ca` is idempotent and loads existing files).

- [ ] **Step 3: Run full suite**

```
uv run pytest -x
```

Expected: green, +1 from the e2e.

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/test_executor_auth_e2e.py
git commit -m "test(e2e): real-TLS register → heartbeat full flow (W3a M3)"
```

---

### Milestone 3 verification (self)

- [ ] Full pytest suite green (0 failures).
- [ ] `tests/e2e/test_executor_auth_e2e.py` exercises real uvicorn TLS and passes.
- [ ] No `require_bearer` remains on any executor/subtask route.
- [ ] `git grep -n "require_bearer" src/dlw/api/` shows only `tasks.py` (UI).

---

## Milestone 4 — Executor side + lint + OpenAPI + PR

After M4: executor side does register/renew/HMAC; lint locks the bearer-free invariant; OpenAPI + runbook updated; PR open.

---

### Task 8: Executor `cert.py` + `auth_lifecycle.py`

**Files:**
- Create: `src/dlw/executor/cert.py`
- Create: `src/dlw/executor/auth_lifecycle.py`
- Create: `tests/executor/test_cert.py`, `tests/executor/test_auth_lifecycle.py`

- [ ] **Step 1: Write failing tests for `cert.py`**

Create `tests/executor/test_cert.py` — cases: `test_build_csr_returns_pem_and_key`, `test_persist_and_load_roundtrip`, `test_fingerprint_matches_controller_format`. `build_csr` returns `(csr_pem, key_pem)`; `persist` writes 4 files; `load` returns the tuple or None; `fingerprint` matches `dlw.auth.ca.fingerprint_of` output format.

- [ ] **Step 2: Implement `src/dlw/executor/cert.py`**

```python
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
```

- [ ] **Step 3: Write failing tests for `auth_lifecycle.py`**

Create `tests/executor/test_auth_lifecycle.py` — at minimum `test_load_or_register_first_run_calls_register` and `test_load_or_register_existing_loads_and_renews`. Mock the controller HTTP calls with `httpx.MockTransport` (W4 pattern). Verify `AuthState` fields populated correctly.

- [ ] **Step 4: Implement `src/dlw/executor/auth_lifecycle.py`**

```python
"""Executor auth lifecycle: register / renew / load (Phase 2 W3a §3.11)."""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from pathlib import Path

import httpx
from cryptography import x509

from dlw.executor import cert as _cert


@dataclass
class AuthState:
    executor_id: str
    epoch: int
    cert_pem: bytes
    key_pem: bytes
    ca_chain_pem: bytes
    jwt: str
    jwt_exp: _dt.datetime
    cert_exp: _dt.datetime
    hmac_seed: bytes
    cert_dir: Path


def _parse_jwt_exp(token: str) -> _dt.datetime:
    import jwt as _pyjwt
    claims = _pyjwt.decode(token, options={"verify_signature": False})
    return _dt.datetime.fromtimestamp(claims["exp"], tz=_dt.UTC)


def _parse_cert_exp(cert_pem: bytes) -> _dt.datetime:
    return x509.load_pem_x509_certificate(cert_pem).not_valid_after_utc


async def register(*, controller_url: str, ca_bundle_path: str | None,
                    enrollment_token: str, executor_id: str, host_id: str,
                    capabilities: dict, cert_dir: Path) -> AuthState:
    csr_pem, key_pem = _cert.build_csr(executor_id)
    verify = ca_bundle_path if ca_bundle_path else True
    async with httpx.AsyncClient(verify=verify) as c:
        r = await c.post(f"{controller_url}/api/v1/executors/register", json={
            "host_id": host_id, "executor_id_proposal": executor_id,
            "capabilities": capabilities,
            "client_csr_pem": csr_pem.decode("utf-8"),
        }, headers={"X-Enrollment-Token": enrollment_token})
        r.raise_for_status()
    body = r.json()
    cert_pem = body["client_cert_pem"].encode("utf-8")
    ca_chain_pem = "\n".join(body["ca_chain"]).encode("utf-8")
    hmac_seed = bytes.fromhex(body["hmac_seed_hex"])
    _cert.persist(cert_dir, cert_pem=cert_pem, key_pem=key_pem,
                  ca_chain_pem=ca_chain_pem, hmac_seed=hmac_seed)
    return AuthState(
        executor_id=body["executor_id"], epoch=body["epoch"],
        cert_pem=cert_pem, key_pem=key_pem, ca_chain_pem=ca_chain_pem,
        jwt=body["executor_jwt"], jwt_exp=_parse_jwt_exp(body["executor_jwt"]),
        cert_exp=_parse_cert_exp(cert_pem), hmac_seed=hmac_seed,
        cert_dir=cert_dir,
    )


async def renew(state: AuthState, *, controller_url: str) -> AuthState:
    """POST /{eid}/renew over mTLS. Include a fresh CSR iff cert TTL < 1h."""
    now = _dt.datetime.now(_dt.UTC)
    payload: dict = {}
    new_key_pem = state.key_pem
    if state.cert_exp - now < _dt.timedelta(hours=1):
        csr_pem, new_key_pem = _cert.build_csr(state.executor_id)
        payload["client_csr_pem"] = csr_pem.decode("utf-8")
    cert_file = state.cert_dir / "client-cert.pem"
    key_file = state.cert_dir / "client-key.pem"
    async with httpx.AsyncClient(
        verify=str(state.cert_dir / "ca-chain.pem"),
        cert=(str(cert_file), str(key_file)),
        headers={"Authorization": f"Bearer {state.jwt}"},
    ) as c:
        r = await c.post(
            f"{controller_url}/api/v1/executors/{state.executor_id}/renew",
            json=payload,
        )
        r.raise_for_status()
    body = r.json()
    new_jwt = body["executor_jwt"]
    cert_pem = state.cert_pem
    cert_exp = state.cert_exp
    if body.get("client_cert_pem"):
        cert_pem = body["client_cert_pem"].encode("utf-8")
        cert_exp = _parse_cert_exp(cert_pem)
        _cert.persist(state.cert_dir, cert_pem=cert_pem, key_pem=new_key_pem,
                      ca_chain_pem=state.ca_chain_pem, hmac_seed=state.hmac_seed)
    return AuthState(
        executor_id=state.executor_id, epoch=state.epoch,
        cert_pem=cert_pem, key_pem=new_key_pem, ca_chain_pem=state.ca_chain_pem,
        jwt=new_jwt, jwt_exp=_parse_jwt_exp(new_jwt), cert_exp=cert_exp,
        hmac_seed=state.hmac_seed, cert_dir=state.cert_dir,
    )


async def load_or_register(*, cert_dir: Path, controller_url: str,
                          ca_bundle_path: str | None, enrollment_token: str,
                          executor_id: str, host_id: str,
                          capabilities: dict) -> AuthState:
    loaded = _cert.load(cert_dir)
    if loaded is not None:
        cert_pem, key_pem, ca_chain_pem, hmac_seed = loaded
        # We have a cert but no JWT (JWT is never persisted). Re-register to
        # get a fresh JWT — simpler + always correct than a JWT-only path.
        # The existing cert is still valid; /register's upsert just bumps epoch.
        # W3a simplification: always re-register on restart.
        return await register(
            controller_url=controller_url, ca_bundle_path=ca_bundle_path,
            enrollment_token=enrollment_token, executor_id=executor_id,
            host_id=host_id, capabilities=capabilities, cert_dir=cert_dir,
        )
    return await register(
        controller_url=controller_url, ca_bundle_path=ca_bundle_path,
        enrollment_token=enrollment_token, executor_id=executor_id,
        host_id=host_id, capabilities=capabilities, cert_dir=cert_dir,
    )
```

> **Simplification noted in code:** `load_or_register` always re-registers on restart (the JWT is never persisted, so a "load + renew" path would still need a valid JWT, which we don't have). Re-register is idempotent (epoch bumps — correct W1 fence semantics). The renew loop handles in-process refresh; restart goes through register. This is simpler and always correct. The two branches above are intentionally identical — kept separate so a future Phase-3 JWT-persistence optimization has an obvious seam.

- [ ] **Step 5: Run the executor auth tests**

```
uv run pytest tests/executor/test_cert.py tests/executor/test_auth_lifecycle.py -v
```

Expected: all pass.

- [ ] **Step 6: Run full suite**

```
uv run pytest -x
```

Expected: green, + the new executor-side tests.

- [ ] **Step 7: Commit**

```bash
git add src/dlw/executor/cert.py src/dlw/executor/auth_lifecycle.py tests/executor/test_cert.py tests/executor/test_auth_lifecycle.py
git commit -m "feat(executor): cert.py + auth_lifecycle.py (register/renew/load) (W3a M4)"
```

---

### Task 9: Executor `client.py` + `runner.py` + `config.py`

**Files:**
- Modify: `src/dlw/executor/config.py`
- Modify: `src/dlw/executor/client.py`
- Modify: `src/dlw/executor/runner.py`
- Modify: `tests/executor/test_client.py`, `tests/executor/test_runner.py`, `tests/executor/test_runner_dispatch.py`, `tests/executor/test_runner_external_throttle.py`

- [ ] **Step 1: Add config fields to `src/dlw/executor/config.py`**

```python
    # Phase 2 W3a — mTLS + JWT auth
    enrollment_token: str = Field(default="")
    executor_cert_dir: str = Field(default="~/.dlw/executor")
    executor_ca_bundle: str = Field(default="")   # runtime-defaults to {cert_dir}/ca-chain.pem
```

Env vars: `DLW_EXECUTOR_ENROLLMENT_TOKEN`, `DLW_EXECUTOR_EXECUTOR_CERT_DIR`, `DLW_EXECUTOR_EXECUTOR_CA_BUNDLE` (the existing `DLW_EXECUTOR_` prefix applies).

- [ ] **Step 2: Rewrite `src/dlw/executor/client.py`**

The `ControllerClient` takes an `AuthState` reference instead of a bearer token. Read the current file first. Key changes:
- Constructor: `ControllerClient(base_url, auth_state, timeout_seconds=30.0, _transport=None)` — drop `bearer_token`.
- Each request builds an `httpx.AsyncClient` with `verify=<ca_chain_path>`, `cert=(<cert_path>, <key_path>)`, `headers={"Authorization": f"Bearer {auth.jwt}"}`. (When `_transport` is injected for tests, `verify` / `cert` are ignored — MockTransport short-circuits.)
- `heartbeat(...)` additionally computes the HMAC headers via `compute_hmac` on the JSON body.
- Add `update_auth(new_state: AuthState)` to swap the auth ref in place (the renew loop calls this).
- `current_epoch()` reads `auth.epoch`.
- Preserve the `_transport` injection seam — tests rely on it.

Write the full file. Mirror the W1 structure (tenacity retry decorator, `__aenter__`/`__aexit__`, the per-method request shape) but swap the auth.

- [ ] **Step 3: Rewrite `src/dlw/executor/runner.py` auth bootstrap + renew loop**

Read the file. Changes:
- `run()`: before the W1 `/join` (now removed), call `load_or_register(...)` → `self._auth`. Pass it into the client (`self._client.update_auth(self._auth)` or construct the client with it).
- Spawn a THIRD background task `_auth_renew_loop` alongside `_heartbeat_loop` + `_poll_and_execute_loop`.
- `_auth_renew_loop`: sleep until `min(jwt_exp - 5min, cert_exp - 1h)`, then `self._auth = await renew(self._auth, controller_url=...)` + `self._client.update_auth(self._auth)`. On exception: log + retry next cycle. On `_shutdown`: return.
- The W1 `EPOCH_MISMATCH` re-join path in `_poll_and_execute_loop`: generalize the 401 handler to call `load_or_register(...)` (which re-registers) instead of the deleted `/join`.

```python
async def _auth_renew_loop(self) -> None:
    from datetime import UTC, datetime, timedelta
    from dlw.executor.auth_lifecycle import renew
    while not self._shutdown.is_set():
        now = datetime.now(UTC)
        jwt_due = self._auth.jwt_exp - timedelta(minutes=5)
        cert_due = self._auth.cert_exp - timedelta(hours=1)
        sleep_for = max(60, int((min(jwt_due, cert_due) - now).total_seconds()))
        try:
            await asyncio.wait_for(self._shutdown.wait(), timeout=sleep_for)
            return
        except asyncio.TimeoutError:
            pass
        try:
            self._auth = await renew(self._auth,
                                     controller_url=self._s.controller_url)
            self._client.update_auth(self._auth)
        except Exception as e:
            logger.warning("auth renew failed: %s; retry next cycle", e)
```

- [ ] **Step 4: Migrate the executor test setups**

`tests/executor/test_client.py` / `test_runner.py` / `test_runner_dispatch.py` / `test_runner_external_throttle.py` construct `ControllerClient` and `ExecutorRunner`. Update:
- `ControllerClient(...)` calls: pass a synthetic `AuthState` (build one with a self-signed cert + a fake JWT + a 32-byte seed) instead of `bearer_token`.
- `ExecutorRunner(...)` calls: the runner now does `load_or_register` in `run()` — for tests that don't call `run()` (most just test `_execute_subtask` / `_choose_downloader`), inject `self._auth` directly after construction or via a constructor param. Choose whichever is least invasive given the current test structure; the W2b1 tests pass `MagicMock()` downloaders, so a `MagicMock()` or a synthetic `AuthState` for `_auth` works.

These are mechanical fixture edits — no test logic changes.

- [ ] **Step 5: Run executor tests**

```
uv run pytest tests/executor/ -v
```

Expected: all pass.

- [ ] **Step 6: Run full suite**

```
uv run pytest -x
```

Expected: green.

- [ ] **Step 7: Commit**

```bash
git add src/dlw/executor/config.py src/dlw/executor/client.py src/dlw/executor/runner.py tests/executor/
git commit -m "feat(executor): client + runner mTLS/JWT/HMAC auth + renew loop (W3a M4)"
```

---

### Task 10: Lint + OpenAPI + operator runbook

**Files:**
- Modify: `tools/lint_invariants.py`
- Modify: `api/openapi.yaml`
- Modify: `docs/operator/`

- [ ] **Step 1: Add `check_no_bearer_on_executor_routes` to `tools/lint_invariants.py`**

After the W2b2 helpers, add:

```python
def check_no_bearer_on_executor_routes() -> list[str]:
    """W3a §3.15: forbid Depends(require_bearer) in executor/subtask route files.
    Those endpoints must use mTLS + JWT (not the UI shared-secret bearer)."""
    errors: list[str] = []
    files = [
        ROOT / "src" / "dlw" / "api" / "executors.py",
        ROOT / "src" / "dlw" / "api" / "subtasks.py",
    ]
    import ast as _ast
    for f in files:
        if not f.exists():
            continue
        src = f.read_text(encoding="utf-8")
        tree = _ast.parse(src)
        for node in _ast.walk(tree):
            # Depends(require_bearer) — a Call to Depends with require_bearer arg
            if (isinstance(node, _ast.Call)
                    and isinstance(node.func, _ast.Name)
                    and node.func.id == "Depends"
                    and node.args
                    and isinstance(node.args[0], _ast.Name)
                    and node.args[0].id == "require_bearer"):
                errors.append(
                    f"{f.relative_to(ROOT)}:{node.lineno}: "
                    f"require_bearer forbidden on executor/subtask routes "
                    f"(use mTLS + JWT)"
                )
    return errors
```

Wire into `main()`: `failures.extend(check_no_bearer_on_executor_routes())`.

- [ ] **Step 2: Run the lint**

```
python tools/lint_invariants.py
uv run pytest tools/test_lint_invariants.py -v
```

Expected: lint exits 0 (the M3 migration removed all `require_bearer` from those files); existing lint self-tests pass.

- [ ] **Step 3: Update `api/openapi.yaml`**

- Remove the `/executors/join` operation.
- Add `/executors/register` (POST, `X-Enrollment-Token` header, `ExecutorRegister` body, `RegistrationResponse` 201).
- Add `/executors/{executorId}/renew` (POST, mTLS + JWT, `RenewResponse` 200).
- On the `/executors/{executorId}/heartbeat` operation: document the `X-HMAC-Timestamp` / `X-HMAC-Nonce` / `X-HMAC-Signature` headers as required.
- Add `RegistrationResponse` / `RenewResponse` / `ExecutorRegister` schemas under `components/schemas`.

Match the existing openapi.yaml indentation + style. The W2b2 changes show the pattern.

- [ ] **Step 4: Update `docs/operator/`**

Add a new file `docs/operator/executor-auth.md` (or append to `executor-runbook.md` if that's the established home — check `ls docs/operator/`):

```markdown
## mTLS + Executor JWT + HMAC (Phase 2 W3a+)

### Controller bootstrap

On first launch the controller generates, under `${DLW_CA_DIR}` (default
`./.ca`, chmod 700):

- `ca-cert.pem` / `ca-key.pem` — the self-signed CA (10-year validity).
- `server-cert.pem` / `server-key.pem` — the controller's TLS server cert
  (SAN: localhost, $DLW_CONTROLLER_HOSTNAME, 127.0.0.1, ::1).
- `jwt-signing.pem` — Ed25519 JWT signing key.
- `enrollment.token` — 256-bit hex token (also logged once at INFO).

Run uvicorn with TLS:

    uvicorn dlw.main:app --host 0.0.0.0 --port 8443 \
      --ssl-keyfile  ${DLW_CA_DIR}/server-key.pem \
      --ssl-certfile ${DLW_CA_DIR}/server-cert.pem \
      --ssl-ca-certs ${DLW_CA_DIR}/ca-cert.pem \
      --ssl-cert-reqs 2

### Enrolling an executor

1. Copy the controller's enrollment token to the executor host out-of-band.
2. Set `DLW_EXECUTOR_ENROLLMENT_TOKEN` on the executor.
3. On first boot the executor generates a keypair, builds a CSR, calls
   `/register`, and persists `client-cert.pem` / `client-key.pem` /
   `ca-chain.pem` / `hmac-seed` under `${DLW_EXECUTOR_CERT_DIR}`
   (default `~/.dlw/executor`, chmod 700).
4. Certs auto-renew (24h cert, 1h JWT) via the executor's renew loop.

### `DLW_TLS_TRUSTED_PROXY` — security warning

`DLW_TLS_TRUSTED_PROXY=1` makes the controller honor the
`X-Client-Cert-PEM` header instead of the direct TLS peer cert. Only
enable this when a real TLS-terminating reverse proxy sits in front AND
the uvicorn port is NOT directly reachable. With it on and no proxy,
anyone can forge the header. Default is `0` (direct uvicorn TLS only).

### Host clock sync

Heartbeats carry an HMAC timestamp validated within ±5 min. Run
`chrony` / `systemd-timesyncd` on all executor + controller hosts.
```

- [ ] **Step 5: Final local verification**

```
python tools/lint_invariants.py
python tools/lint_no_direct_status_write.py
uv run pytest -x
```

Expected: both lints clean; pytest green.

- [ ] **Step 6: Commit**

```bash
git add tools/lint_invariants.py api/openapi.yaml docs/operator/
git commit -m "ci(lint): forbid bearer on executor routes + OpenAPI + operator runbook (W3a M4)"
```

---

### Task 11: Push branch + open PR + monitor CI (controller does this)

- [ ] **Step 1: Confirm branch state**

```bash
git status
git log main..HEAD --oneline
```

Expected: clean working tree; ~11 commits (1 spec + 10 task commits).

- [ ] **Step 2: Push**

```bash
git push -u origin feat/phase-2-w3a-mtls-jwt-hmac
```

- [ ] **Step 3: Open the PR**

```bash
gh pr create \
  --title "Phase 2 Week 3a — mTLS + executor JWT + HMAC heartbeat" \
  --body "$(cat <<'EOF'
## Summary

W3a of `docs/v2.0/08-mvp-roadmap.md` §2.6 Day 1-3 — replaces executor-side bearer auth with SVID-style mTLS + Ed25519 JWT + HMAC heartbeat (SEC-01 + SEC-04):

- **mTLS substrate.** Controller bootstraps a self-signed CA + server cert + Ed25519 JWT signing key under `${DLW_CA_DIR}` (file-persisted, chmod 600). New `POST /executors/register` (enrollment-token auth) signs an executor CSR; `POST /executors/{eid}/renew` refreshes the JWT (+ cert when a CSR is supplied). W1 `/join` deleted.
- **JWT + HMAC.** Three chained FastAPI deps — `require_executor_mtls` → `require_executor_jwt` → `require_hmac_heartbeat`. `require_executor_epoch` refactored to chain under the JWT dep and assert the path id matches the mTLS identity (confused-deputy guard). In-process nonce store bounds replay to a ±5min window.
- **Executor side.** New `cert.py` + `auth_lifecycle.py`; `client.py` does mTLS + JWT + HMAC; runner spawns a 3rd background loop for cert/JWT renewal. `load_or_register` re-registers on restart (idempotent epoch bump).
- **UI auth unchanged.** `/api/v1/tasks/*` keeps `require_bearer`; a new `check_no_bearer_on_executor_routes` lint locks executor routes onto mTLS+JWT.

Spec: `docs/superpowers/specs/2026-05-14-phase-2-w3a-mtls-jwt-hmac-design.md`.
Plan: `docs/superpowers/plans/2026-05-14-phase-2-w3a-mtls-jwt-hmac.md`.

W3b (HF reverse-proxy) and W3c (active/standby) are companion specs.

## Test plan

- [x] Backend pytest: baseline 181 + ~27 new + ~13-15 migrated W1 setups. Zero regressions.
- [x] `dlw.auth.{ca,jwt_signing,hmac_nonce}` modules + 3 FastAPI deps unit-tested.
- [x] `/register` + `/renew` endpoint tests.
- [x] One real-TLS e2e (uvicorn subprocess with `--ssl-*`): register → HMAC-signed heartbeat over mTLS.
- [x] alembic upgrade clean from W2b2 head; downgrade clean.
- [x] `tools/lint_invariants.py` `check_no_bearer_on_executor_routes` returns 0.
- [x] `cryptography` + `pyjwt[crypto]` added to `pyproject.toml` + `uv.lock`.
- [x] OpenAPI: `/register` + `/renew` added, `/join` removed, HMAC headers documented.

## Out of scope (deferred — see spec §1.2)

HF reverse-proxy (W3b); active/standby + chaos drill (W3c); OIDC / multi-tenant / UI auth (Phase 3); Vault/KMS for keys (Phase 3); CRL / cert-manager (Phase 3+); envelope encryption of hmac_seed (Phase 3); PG/Redis nonce store (Phase 3).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Monitor CI**

```bash
gh pr checks $(gh pr view --json number -q .number) --watch
```

Expected: 12 checks pass. If any fail:

- **pytest** — the real-TLS e2e is the most fragile; if it fails on CI but passes locally, check the uvicorn subprocess startup timeout + the DB env var plumbing into the subprocess.
- **Invariant + cross-ref lint** — `check_no_bearer_on_executor_routes` may catch a missed `require_bearer`; remove it from the source.
- **OpenAPI lint** — spectral may flag the new operations; diff against the W2b2 OpenAPI change pattern.

---

### Milestone 4 verification (self)

- [ ] PR opened; CI 12/12 green.
- [ ] No diff outside the File Structure list (`gh pr diff --name-only`).
- [ ] `git grep require_bearer src/dlw/api/` shows only `tasks.py`.
- [ ] All new tests pass; no W1/W2a/W2b1/W2b2 regressions.

---

## Definition of Done

- [ ] All 10 implementation tasks committed on `feat/phase-2-w3a-mtls-jwt-hmac`.
- [ ] PR opened, CI 12/12 green.
- [ ] `cryptography` + `pyjwt[crypto]` pinned in `pyproject.toml`; `uv.lock` updated.
- [ ] 1 alembic migration applies + reverses clean; `executors.hmac_seed_encrypted` exists.
- [ ] `dlw.auth.{ca,jwt_signing,hmac_nonce}` + 3 FastAPI deps + `require_executor_epoch` refactor — all unit-tested.
- [ ] `/register` + `/renew` endpoints work; `/join` + `ExecutorJoin` deleted.
- [ ] `join_executor` → `upsert_executor_with_cert` (writes cert_fingerprint + hmac_seed).
- [ ] Executor side: `cert.py` + `auth_lifecycle.py` + rewritten `client.py` + `runner.py` 3rd loop.
- [ ] One real-TLS e2e passes.
- [ ] `check_no_bearer_on_executor_routes` lint reports 0; `git grep require_bearer src/dlw/api/` → only `tasks.py`.
- [ ] OpenAPI updated; operator runbook documents CA dir, enrollment token, uvicorn `--ssl-*`, `DLW_TLS_TRUSTED_PROXY` warning, clock-sync requirement.
- [ ] No new CI jobs. Full suite green.

---

## Plan Revisions Log

(Empty on first draft.)

| Tag | Severity | Issue | Fix applied |
|-----|----------|-------|-------------|
| _(none yet)_ | | | |

---

## References

- Spec: `docs/superpowers/specs/2026-05-14-phase-2-w3a-mtls-jwt-hmac-design.md`
- Predecessor specs/plans: W1 / W2a / W2b1 / W2b2 under `docs/superpowers/{specs,plans}/`
- Security: `docs/v2.0/04-security-and-tenancy.md` §2.2; Protocol: `docs/v2.0/02-protocol.md` §4.1
- Roadmap: `docs/v2.0/08-mvp-roadmap.md` §2.6 Phase 2 W3 Day 1-3
- W2b2 PR (merged): https://github.com/l17728/modelpull/pull/11 (squash `ba89a91`)
