# Phase 2 Week 3a — mTLS + Executor JWT + HMAC Heartbeat Design

> **Status:** Draft (brainstormed 2026-05-14).
> **Companion plan:** `docs/superpowers/plans/2026-05-14-phase-2-w3a-mtls-jwt-hmac.md` (to be written by writing-plans skill after spec approval).
> **Roadmap source:** `docs/v2.0/08-mvp-roadmap.md` §2.6 — Phase 2 Week 3 Day 1-3 (mTLS CA + enrollment + JWT, then HMAC heartbeat).
> **Companion split (W3b / W3c):** HF reverse-proxy is W3b; Active/standby controller + chaos drill is W3c. Both depend on W3a's auth substrate.
> **Security source:** `docs/v2.0/04-security-and-tenancy.md` §2.2 (Executor auth, SEC-01 + SEC-04) + `docs/v2.0/02-protocol.md` §4.1 (heartbeat HMAC).

---

## 1. Goal & Non-Goals

### 1.1 Goal

Replace the Phase-1 single-shared-bearer auth for **executor** endpoints with the SVID-style auth specified in `04 §2.2`:

1. **mTLS substrate.** Controller bootstraps a self-signed CA at first startup (file-persisted under `${DLW_CA_DIR}`, chmod 600). A new `POST /api/v1/executors/register` endpoint (auth: enrollment token) accepts an executor-generated CSR + metadata, signs a 24h client cert, persists the cert fingerprint on `executors.cert_fingerprint`, and returns `(client_cert_pem, ca_chain, initial_jwt, hmac_seed)`. uvicorn loads the CA bundle at startup; a FastAPI dependency reads the verified peer cert and looks up the executor by fingerprint.

2. **Executor JWT.** Controller generates an Ed25519 signing keypair at first startup (same dir as the CA). `POST /api/v1/executors/{eid}/renew` requires a valid client cert + current JWT and returns a fresh 1h JWT (plus a new client cert if the old cert is within 1h of expiry). JWT claims: `{iss, sub: executor_id, epoch, scope, iat, exp}`.

3. **HMAC heartbeat.** Per `04 §2.2.4` / `02 §4.1`: every heartbeat body is signed `HMAC-SHA256(hmac_seed, f"{ts}:{nonce}:" + body)`; the executor sends `X-HMAC-Timestamp`, `X-HMAC-Nonce`, `X-HMAC-Signature` headers. The controller validates: timestamp within ±5 min, nonce not seen in an in-process LRU (size 10000, TTL 5 min), signature matches constant-time.

4. **Executor-side lifecycle.** First boot: read enrollment token from env, generate an Ed25519 keypair, build a CSR, call `/register`, persist `client-cert.pem` + `client-key.pem` + `ca-chain.pem` + `hmac-seed` (chmod 600), cache the JWT in memory. A background renewal loop refreshes the JWT ~5 min before expiry and the client cert ~1 h before expiry.

After W3a: executor endpoints (`/heartbeat`, `/poll`, `/report`) require mTLS + valid JWT + (heartbeat only) HMAC signature. UI endpoints (`/api/v1/tasks/*`) keep `require_bearer` — Phase 3 W3 (User OIDC) replaces those.

### 1.2 Non-goals (deferred — explicit list)

| Item | Where |
|---|---|
| HF reverse-proxy + executor route migration | **W3b** |
| Active/standby controller + chaos drill | **W3c** |
| OIDC + multi-tenant + UI auth migration | **Phase 3 W3** |
| `tenants.hf_tokens` envelope encryption + 5-min controller-memory cache | **Phase 3** (consumed by W3b) |
| Vault / k8s Secret integration for CA + signing key + enrollment token | **Phase 3** |
| cert-manager / Sigstore / CRL (cert revocation) | **Phase 3+** |
| Audit log of register / renew events | **Phase 3** |
| Per-tenant CA (each tenant has its own SVID issuer) | **Phase 3+** |
| Rotation of the CA itself (re-sign all executors) | **Phase 3** ops |
| Envelope encryption of `hmac_seed` at rest (column is forward-compatible; W3a stores raw bytes) | **Phase 3** |
| Dual-auth transition window (bearer + mTLS in parallel) | not needed — internal beta tolerates hard cutover |
| WebSocket auth (UI WS subscription) | Phase 1 W3 already uses bearer; revisit Phase 3 |
| PG-backed / Redis nonce store (survives controller restart) | **Phase 3** — W3a's in-process store is bounded by the ±5min window |

---

## 2. Tech Stack Additions

W3a **adds two runtime dependencies** (correcting an earlier assumption — `cryptography` is currently only a transitive dep of boto3/httpx, and no JWT library is present):

| Package | Version pin | Why |
|---|---|---|
| `cryptography` | `>=43,<44` | Promoted from transitive to explicit — W3a uses `cryptography.x509` + `cryptography.hazmat` directly for CA generation, CSR signing, Ed25519 keys. Pinning it explicitly is correct hygiene now that it's a first-class dependency. |
| `pyjwt[crypto]` | `>=2.9,<3.0` | EdDSA (Ed25519) JWT signing + verification. PyJWT is lighter than `python-jose` and its `[crypto]` extra reuses the same `cryptography` backend. |

No new dev dependencies (`pytest` + existing fixtures). No new CI jobs. uvicorn (already pinned) provides the TLS termination via its `--ssl-*` flags.

`uv add cryptography pyjwt` updates `pyproject.toml` + `uv.lock`. Per the `feedback_uv_ci_version_pin` memory: the CI `uv` is pinned to 0.11.9 — these are standard `dependencies` entries, not PEP 735 groups, so no `--all-groups`-style incompatibility.

---

## 3. Components

### 3.1 New: `src/dlw/auth/ca.py`

Self-signed CA generation, CSR signing, fingerprint extraction, **and the controller's own server cert** (for uvicorn TLS).

```python
@dataclass(frozen=True)
class CABundle:
    cert_pem: bytes
    key_pem: bytes
    cert: x509.Certificate
    key: ed25519.Ed25519PrivateKey


def bootstrap_ca(ca_dir: Path) -> CABundle:
    """Idempotent: load existing CA from disk, else generate + persist.
    Files: ca-cert.pem, ca-key.pem (chmod 600). CA validity 10 years."""


def sign_csr(ca: CABundle, csr_pem: bytes, executor_id: str, ttl_hours: int = 24) -> bytes:
    """Sign an executor CSR. CN = executor_id. SAN carries
    URI:spiffe://dlw/executor/<id>. ExtendedKeyUsage = CLIENT_AUTH.
    Raises ValueError on invalid CSR signature."""


def fingerprint_of(cert_pem: bytes) -> str:
    """SHA256 fingerprint as 'SHA256:<hex>' — stored on executors.cert_fingerprint."""
```

Implementation notes:

- CA + executor + server certs all use **Ed25519** keys (`ed25519.Ed25519PrivateKey`). `cryptography` signs Ed25519 certs with `.sign(key, None)` (the hash-algorithm arg is `None` for Ed25519).
- CA cert: `BasicConstraints(ca=True, path_length=0)`, `KeyUsage(key_cert_sign=True, crl_sign=True)`.
- Executor cert: `BasicConstraints(ca=False)`, `KeyUsage(digital_signature=True)`, `ExtendedKeyUsage([CLIENT_AUTH])`, SAN `URI:spiffe://dlw/executor/<id>`.
- `sign_csr` validates `csr.is_signature_valid` before signing.

#### 3.1.1 Server cert (`_ensure_server_cert`)

`bootstrap_ca` is paired with a server-cert helper used at the same bootstrap point:

```python
def ensure_server_cert(ca: CABundle, ca_dir: Path,
                       hostname: str = "dlw-controller") -> tuple[Path, Path]:
    """Idempotent: load or generate server-cert.pem + server-key.pem (chmod 600).

    CN = hostname. SubjectAlternativeName MUST include:
      - DNS:localhost
      - DNS:<hostname>            (from $DLW_CONTROLLER_HOSTNAME, default 'dlw-controller')
      - IP:127.0.0.1
      - IP:::1
    TTL = 10 years (matches CA — server cert is not rotated in Phase 2).
    ExtendedKeyUsage = SERVER_AUTH.

    Returns (server_cert_path, server_key_path) for the uvicorn --ssl-* flags.
    """
```

The SAN list is the load-bearing detail: without `DNS:localhost` + `IP:127.0.0.1`, an executor connecting to `https://localhost:8443` fails httpx hostname verification. The implementer MUST include all four SAN entries. `hostname` comes from `DLW_CONTROLLER_HOSTNAME` env (default `dlw-controller`); operators set it to the real hostname in prod, and the cert is regenerated only if absent.

### 3.2 New: `src/dlw/auth/jwt_signing.py`

Ed25519 JWT signing + verification via **PyJWT**.

```python
@dataclass(frozen=True)
class JWTKeypair:
    priv_pem: bytes
    pub_pem: bytes


def bootstrap_keypair(ca_dir: Path) -> JWTKeypair:
    """Idempotent: load or generate jwt-signing.pem (chmod 600, PKCS8 Ed25519
    private key). Public key is derived on load."""


def sign(kp: JWTKeypair, *, executor_id: str, epoch: int,
         scopes: list[str], ttl_seconds: int = 3600) -> str:
    """jwt.encode({iss:'dlw-controller', sub:executor_id, epoch, scope:' '.join(scopes),
    iat, exp}, kp.priv_pem, algorithm='EdDSA')."""


def verify(kp: JWTKeypair, token: str) -> dict[str, Any]:
    """jwt.decode(token, kp.pub_pem, algorithms=['EdDSA'], issuer='dlw-controller',
    options={'require': ['sub','epoch','scope','exp','iss','iat']}).
    Raises jwt.PyJWTError on any failure (signature / expiry / shape / issuer)."""
```

PyJWT API: `jwt.encode(payload, key, algorithm="EdDSA")` accepts a PEM-encoded Ed25519 private key; `jwt.decode(token, key, algorithms=["EdDSA"], ...)` accepts the PEM public key. The `exp` claim is validated automatically by PyJWT; `issuer=` triggers `iss` validation; `options={"require": [...]}` enforces claim presence.

### 3.3 New: `src/dlw/auth/hmac_nonce.py`

```python
class NonceStore:
    """In-process LRU with timestamp-based eviction. asyncio single-threaded —
    no lock needed. Restart loses state; replay defense is bounded by the
    ±5min timestamp window enforced at the dependency layer."""

    def __init__(self, *, maxsize: int = 10_000, ttl_seconds: int = 300) -> None: ...
    def seen(self, nonce: str) -> bool: ...     # evicts expired, then checks membership
    def add(self, nonce: str) -> None: ...      # evicts expired + LRU-trims, then inserts


def compute_hmac(hmac_seed: bytes, *, ts: int, nonce: str, body: bytes) -> str:
    """HMAC-SHA256(hmac_seed, f'{ts}:{nonce}:'.encode() + body).hexdigest()."""


def verify_hmac(hmac_seed: bytes, *, ts: int, nonce: str, body: bytes,
                signature_hex: str) -> bool:
    """hmac.compare_digest(compute_hmac(...), signature_hex) — constant-time."""
```

`NonceStore` uses an `OrderedDict[str, float]` keyed by nonce, value = `time.monotonic()` insertion time. `seen()` and `add()` both call `_evict_expired()` first (pop from the front while value < `now - ttl`). `add()` also LRU-trims when `len >= maxsize`.

### 3.4 New: FastAPI dependencies

Three chained dependencies. Each builds on the prior:

**`src/dlw/auth/executor_mtls.py` — `require_executor_mtls`:**
- Reads the verified peer cert from `request.scope` (uvicorn TLS path: `request.scope["transport"].get_extra_info("peercert")` → DER → PEM) OR, when `DLW_TLS_TRUSTED_PROXY=1`, from the `X-Client-Cert-PEM` header (reverse-proxy / test path).
- Computes the fingerprint, looks up `executors` by `cert_fingerprint`. 401 on missing cert or unknown fingerprint.
- Returns the `Executor` row.

**`src/dlw/auth/executor_jwt_dep.py` — `require_executor_jwt`:**
- `Depends(require_executor_mtls)` → has the `Executor` row.
- Reads `Authorization: Bearer <jwt>`; `jwt_signing.verify(app.state.jwt_keypair, token)`. 401 on any `PyJWTError`.
- Asserts `claims["sub"] == ex.id`. 401 on mismatch.
- Returns the `Executor` row.

**`src/dlw/auth/hmac_heartbeat_dep.py` — `require_hmac_heartbeat`:**
- `Depends(require_executor_jwt)` → has the `Executor` row.
- Reads `X-HMAC-Timestamp` / `X-HMAC-Nonce` / `X-HMAC-Signature` (all required headers — 422 if absent).
- `abs(now - ts) > 300` → 401 `CLOCK_SKEW`.
- `app.state.nonce_store.seen(nonce)` → 401 `REPLAY_DETECTED`.
- `verify_hmac(hmac_seed, ts=, nonce=, body=await request.body(), signature_hex=)` → 401 `HMAC_INVALID` on mismatch.
- On success: `nonce_store.add(nonce)`, return the `Executor` row.
- `hmac_seed` comes from `_decrypt_hmac_seed(ex)` — Phase 2 returns `bytes(ex.hmac_seed_encrypted)` raw; Phase 3 swaps for a KMS decrypt. 401 `HMAC_SEED_MISSING` if the column is NULL (executor must re-register).

**`require_executor_epoch` is refactored** (not "unchanged"). W1's version takes the path `executor_id` + `X-Executor-Epoch` header and does a *fresh* DB lookup by path id. Under W3a that fresh lookup is a confused-deputy gap: an attacker with a valid cert + JWT for executor **A** could hit `/executors/B/heartbeat` with B's epoch, and the W1 dep would happily validate against B's row. The W3a `require_executor_epoch`:

1. `Depends(require_executor_jwt)` → receives the `Executor` row already loaded from the mTLS cert fingerprint (call it `ex_mtls`).
2. Asserts the path `executor_id` parameter equals `ex_mtls.id` → 401 `EXECUTOR_ID_MISMATCH` otherwise. This binds the URL to the authenticated identity.
3. Compares `X-Executor-Epoch` against `ex_mtls.epoch` (W1's fence check) — using the already-loaded row, no second lookup.
4. Returns `ex_mtls`.

The W1 fence semantics (epoch must match) are preserved; the change is *which* row the epoch is checked against (the mTLS-authenticated row, not a path-id lookup) plus the new path-vs-identity assertion.

### 3.5 Modified: `src/dlw/api/executors.py`

**Deleted:** `POST /api/v1/executors/join` (the W1 bearer-auth endpoint).

**New `POST /api/v1/executors/register`** — enrollment-token auth, signs the CSR, INSERTs-or-bumps the executor row (mirrors W1 `join_executor`'s `pg_insert ... ON CONFLICT DO UPDATE` epoch-bump semantics), generates a 256-bit `hmac_seed`, returns `RegistrationResponse`.

**New `POST /api/v1/executors/{eid}/renew`** — `Depends(require_executor_jwt)`. Signs a fresh JWT. If the peer cert TTL is within 1h, re-signs the cert (reusing the peer cert's public key) and updates `executors.cert_fingerprint`. Returns `RenewResponse`.

**Modified `POST /api/v1/executors/{eid}/heartbeat`** — drops `Depends(require_bearer)`; the dependency chain is `require_executor_epoch` (W1) + `require_hmac_heartbeat` (which transitively pulls `require_executor_jwt` → `require_executor_mtls`). Body shape (`ExecutorHeartbeat`) unchanged.

**Modified `POST /api/v1/executors/{eid}/poll`** — drops `Depends(require_bearer)`; chain is `require_executor_epoch` + `require_executor_jwt`. Body shape unchanged.

### 3.6 Modified: `src/dlw/api/subtasks.py`

`POST /api/v1/subtasks/{id}/report` — drops `Depends(require_bearer)`; chain is `require_executor_jwt` + `require_executor_epoch`. Body shape (`SubTaskReport`) unchanged.

### 3.7 New schemas in `src/dlw/schemas/executor.py`

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


class RenewResponse(BaseModel):
    executor_jwt: str
    jwt_renew_in_seconds: int
    client_cert_pem: str | None      # non-null only when cert was rotated
    cert_renew_in_seconds: int | None
```

The W1 `ExecutorJoin` schema is deleted alongside the `/join` endpoint.

### 3.8 Modified: `src/dlw/services/executor_service.py`

W1's `join_executor` (the `pg_insert ... ON CONFLICT DO UPDATE` INSERT-or-bump) is **renamed and extended** to `upsert_executor_with_cert(session, *, executor_id, host_id, capabilities, cert_fingerprint, hmac_seed)`. Same atomic INSERT-or-bump semantics — epoch starts at 1 on insert, bumps by 1 on conflict — but now also writes `cert_fingerprint` and `hmac_seed_encrypted`. `record_heartbeat` is unchanged (W2b1 already routes it through `transition_executor`).

The W3a `/register` endpoint calls `upsert_executor_with_cert`. `join_executor`'s callers (W1 tests) migrate to `upsert_executor_with_cert` or to the `/register` HTTP path.

### 3.9 Modified: `src/dlw/main.py`

`lifespan` startup gains a bootstrap block (before the W1 recovery routine):

```python
from dlw.auth.ca import bootstrap_ca, ensure_server_cert
from dlw.auth.jwt_signing import bootstrap_keypair
from dlw.auth.hmac_nonce import NonceStore

ca_dir = Path(settings.ca_dir)
ca_dir.mkdir(mode=0o700, exist_ok=True)
ca = bootstrap_ca(ca_dir)
ensure_server_cert(ca, ca_dir, hostname=settings.controller_hostname)
jwt_kp = bootstrap_keypair(ca_dir)
enrollment_token = _ensure_enrollment_token(ca_dir, settings)
app.state.ca = ca
app.state.jwt_keypair = jwt_kp
app.state.nonce_store = NonceStore(maxsize=10_000, ttl_seconds=300)
app.state.enrollment_token = enrollment_token
```

`_ensure_enrollment_token`: if `DLW_ENROLLMENT_TOKEN` env is set, use it; else read `${ca_dir}/enrollment.token`; else generate a 256-bit hex token, write the file (chmod 600), and log it once at INFO so the operator can copy it out-of-band.

The W2a/W2b2 `_sweep_loop_main` and W1 recovery routine are unchanged. uvicorn `--ssl-*` flags are passed at the CLI / deployment layer (documented in `docs/operator/`), not in `create_app`.

### 3.10 New: `src/dlw/executor/cert.py`

```python
def build_csr(executor_id: str) -> tuple[bytes, bytes]:
    """Generate an Ed25519 keypair + CSR. CN = executor_id.
    Returns (csr_pem, private_key_pem)."""

def persist(cert_dir: Path, *, cert_pem: bytes, key_pem: bytes,
            ca_chain_pem: bytes, hmac_seed: bytes) -> None:
    """Write client-cert.pem / client-key.pem / ca-chain.pem / hmac-seed
    (all chmod 600) into cert_dir (chmod 700)."""

def load(cert_dir: Path) -> tuple[bytes, bytes, bytes, bytes] | None:
    """Return (cert_pem, key_pem, ca_chain_pem, hmac_seed) or None if absent."""

def fingerprint(cert_pem: bytes) -> str:
    """SHA256:<hex> — same format as controller's ca.fingerprint_of."""
```

### 3.11 New: `src/dlw/executor/auth_lifecycle.py`

```python
@dataclass
class AuthState:
    executor_id: str
    epoch: int
    cert_pem: bytes
    key_pem: bytes
    ca_chain_pem: bytes
    jwt: str
    jwt_exp: datetime
    cert_exp: datetime
    hmac_seed: bytes
    cert_dir: Path           # for the httpx cert= file paths


async def register(*, controller_url, ca_bundle_path, enrollment_token,
                   executor_id, host_id, capabilities, cert_dir) -> AuthState:
    """Build CSR, POST /register, persist cert+key+ca+seed to cert_dir,
    return AuthState with the parsed JWT/cert expiry timestamps."""


async def renew(state: AuthState, *, controller_url) -> AuthState:
    """POST /{eid}/renew using the current cert (mTLS) + JWT. Update the JWT;
    if the response carries a new cert, persist it + update the fingerprint
    timestamps. Return a fresh AuthState."""


async def load_or_register(*, cert_dir, controller_url, ca_bundle_path,
                          enrollment_token, executor_id, host_id,
                          capabilities) -> AuthState:
    """If cert_dir has a persisted cert: load it + run renew() to refresh the
    JWT. Else: run register(). Returns a ready AuthState."""
```

### 3.12 Modified: `src/dlw/executor/client.py`

`ControllerClient` takes an `AuthState` reference (mutable — the renewal loop updates it in place). Each request builds an `httpx.AsyncClient(verify=<ca_chain_path>, cert=(<cert_path>, <key_path>), headers={"Authorization": f"Bearer {jwt}"})`. The `heartbeat` method additionally computes the HMAC headers (`X-HMAC-Timestamp` / `X-HMAC-Nonce` / `X-HMAC-Signature`) over the JSON body via `compute_hmac`.

### 3.13 Modified: `src/dlw/executor/runner.py`

`run()` gains an auth-bootstrap step before `/join` (now `load_or_register`) and spawns a **third** background task `_auth_renew_loop` alongside `_heartbeat_loop` and `_poll_and_execute_loop`:

```python
async def _auth_renew_loop(self) -> None:
    while not self._shutdown.is_set():
        now = datetime.now(UTC)
        jwt_due = self._auth.jwt_exp - timedelta(minutes=5)
        cert_due = self._auth.cert_exp - timedelta(hours=1)
        sleep_for = max(60, int((min(jwt_due, cert_due) - now).total_seconds()))
        try:
            await asyncio.wait_for(self._shutdown.wait(), timeout=sleep_for)
            return   # shutdown
        except asyncio.TimeoutError:
            pass
        try:
            self._auth = await renew(self._auth, controller_url=self._s.controller_url)
            self._client.update_auth(self._auth)
        except Exception as e:
            logger.warning("auth renew failed: %s; retry next cycle", e)
```

The W1 `EPOCH_MISMATCH` re-join path in `_poll_and_execute_loop` is generalized: on a 401 from a protected endpoint, the loop attempts `load_or_register` (which falls back to `/register` with the persisted enrollment token) before giving up — same structure as the existing rejoin logic.

### 3.14 Modified: `src/dlw/executor/config.py` + `src/dlw/config.py`

Executor `ExecutorSettings` adds:

```python
enrollment_token: str = Field(default="", description="OOB enrollment token from operator.")
executor_cert_dir: str = Field(default="~/.dlw/executor")
executor_ca_bundle: str = Field(default="")   # defaults to {cert_dir}/ca-chain.pem at runtime
```

Controller `Settings` adds:

```python
ca_dir: str = Field(default="./.ca")
enrollment_token: str = Field(default="")     # if empty, bootstrap generates one
controller_hostname: str = Field(default="dlw-controller")
tls_trusted_proxy: bool = Field(default=False)   # DLW_TLS_TRUSTED_PROXY
```

### 3.15 Modified: `tools/lint_invariants.py`

New helper `check_no_bearer_on_executor_routes`: AST-scans `src/dlw/api/executors.py` + `src/dlw/api/subtasks.py` for any `Depends(require_bearer)` in a route decorator's `dependencies=[...]` list or as a parameter default. Any hit → failure. Wired into `main()` next to the W2b2 helpers. This locks the migration in — a future change that re-adds bearer to an executor route gets caught.

---

## 4. Schema Changes

**One alembic migration** (`<rev>_p2w3a_hmac_seed.py`):

```python
def upgrade() -> None:
    op.add_column(
        "executors",
        sa.Column("hmac_seed_encrypted", sa.LargeBinary(), nullable=True),
    )

def downgrade() -> None:
    op.drop_column("executors", "hmac_seed_encrypted")
```

`down_revision` = `b1d5ea4944ba` (W2b2 `last_paused_at`). Nullable — pre-W3a executor rows survive with `hmac_seed_encrypted=NULL`; they re-register on next runner restart. ORM: add `hmac_seed_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)` to `Executor`.

`executors.cert_fingerprint` (W1) and `executors.epoch` (W1) already exist. No other DDL.

---

## 5. Wire Format Changes

### 5.1 New endpoint `POST /api/v1/executors/register`

| Aspect | Value |
|---|---|
| Auth | `X-Enrollment-Token` header (no mTLS, no JWT) |
| Request | `ExecutorRegister` (host_id, executor_id_proposal, capabilities, client_csr_pem) |
| Response 201 | `RegistrationResponse` (cert + ca_chain + jwt + hmac_seed_hex + renew intervals) |
| 200 | Same body, returned when re-registering an existing executor_id (epoch bumped) |
| 401 | Invalid / missing enrollment token |
| 422 | Malformed CSR |

### 5.2 New endpoint `POST /api/v1/executors/{eid}/renew`

| Aspect | Value |
|---|---|
| Auth | mTLS peer cert + `Authorization: Bearer <current JWT>` |
| Request | empty `{}` |
| Response 200 | `RenewResponse` (new jwt; new cert iff old cert TTL < 1h) |
| 401 | Missing/invalid mTLS cert, expired/invalid JWT, or `sub`/fingerprint mismatch |

### 5.3 `POST /api/v1/executors/join` — DELETED

The W1 bearer-auth `/join` endpoint and its `ExecutorJoin` schema are removed entirely. No transitional alias.

### 5.4 Heartbeat headers (additive)

`POST /api/v1/executors/{eid}/heartbeat` body shape unchanged; new **required** headers: `Authorization: Bearer <jwt>` (was: shared-secret bearer), `X-HMAC-Timestamp`, `X-HMAC-Nonce`, `X-HMAC-Signature`. `X-Executor-Epoch` (W1) still required.

### 5.5 Poll / Report auth (no body change)

`POST /api/v1/executors/{eid}/poll` and `POST /api/v1/subtasks/{id}/report` switch the `Authorization` header from the shared-secret bearer to the executor JWT. Body shapes unchanged.

### 5.6 OpenAPI

`api/openapi.yaml`: add `/executors/register` + `/executors/{executorId}/renew` operations; remove `/executors/join`; document the HMAC headers on the heartbeat operation; add `RegistrationResponse` / `RenewResponse` / `ExecutorRegister` schemas. The aspirational `04 §2.2.1` doc already sketches the register shape — align with it.

---

## 6. Error Handling Matrix

| Situation | Behaviour |
|---|---|
| `/register` with bad enrollment token | 401 `invalid enrollment token` |
| `/register` with malformed CSR | 422; `sign_csr` raises `ValueError`, endpoint maps to 422 |
| `/register` re-register of existing executor_id | 200; `upsert_executor_with_cert` bumps epoch + regenerates hmac_seed + new cert (W1 fence semantics — old-epoch in-flight subtasks get reclaimed by the W2a sweeper) |
| Protected endpoint, no mTLS peer cert | 401 `missing or invalid mTLS peer cert` (from `require_executor_mtls`) |
| Protected endpoint, peer cert fingerprint not in DB | 401 `cert fingerprint not registered` |
| Protected endpoint, expired JWT | 401 from `require_executor_jwt` (`PyJWTError`); executor's renew loop or 401-handler re-registers |
| Protected endpoint, JWT `sub` ≠ mTLS executor_id | 401 `JWT sub mismatch` — cert + token belong to different executors |
| Heartbeat, timestamp skew > 5min | 401 `CLOCK_SKEW` |
| Heartbeat, nonce already in store | 401 `REPLAY_DETECTED` |
| Heartbeat, signature mismatch | 401 `HMAC_INVALID` |
| Heartbeat, `hmac_seed_encrypted` is NULL (pre-W3a row) | 401 `HMAC_SEED_MISSING` — executor must re-register |
| Path `executor_id` ≠ mTLS-authenticated executor (confused deputy) | 401 `EXECUTOR_ID_MISMATCH` from the refactored `require_executor_epoch` (§3.4) |
| `/renew`, peer cert valid but JWT expired | 401 — `/renew` requires a *valid* JWT; if the JWT genuinely expired, the executor re-registers via the enrollment token |
| Controller restart loses nonce store | A captured heartbeat with a ≤5min-old timestamp replays exactly once post-restart; impact is limited to one idempotent `last_heartbeat_at` write — no state corruption |
| uvicorn TLS not configured but `DLW_TLS_TRUSTED_PROXY=0` | `require_executor_mtls` finds no peer cert + no trusted header → 401 on every protected request — fails closed |
| Executor clock ahead/behind controller | `CLOCK_SKEW` on heartbeat; operator runs `chrony`/`systemd-timesyncd` (standard) |
| Server cert SAN missing `localhost` / `127.0.0.1` | Executor httpx `verify=` fails the TLS handshake — `ensure_server_cert` MUST include all four SAN entries (§3.1.1) |

---

## 7. Testing Strategy

### 7.1 Unit + integration (~25 new cases)

| # | File | Case | What it asserts |
|---|---|---|---|
| 1 | `tests/auth/test_ca.py` | `test_bootstrap_ca_idempotent` | Two calls on same dir → identical cert (load path) |
| 2 | same | `test_sign_csr_returns_valid_client_cert` | CN == executor_id, 24h TTL, signed by CA, EKU=CLIENT_AUTH |
| 3 | same | `test_fingerprint_of_is_deterministic_sha256` | "SHA256:<hex>" format, stable for same cert |
| 4 | same | `test_ensure_server_cert_has_required_sans` | SAN includes DNS:localhost, DNS:<hostname>, IP:127.0.0.1, IP:::1 |
| 5 | `tests/auth/test_jwt_signing.py` | `test_bootstrap_keypair_idempotent` | Second call → same keypair |
| 6 | same | `test_sign_and_verify_roundtrip` | sign → verify returns matching claims |
| 7 | same | `test_verify_rejects_expired_token` | `exp` in the past → PyJWTError |
| 8 | same | `test_verify_rejects_wrong_issuer` | tampered `iss` → PyJWTError |
| 9 | `tests/auth/test_hmac_nonce.py` | `test_hmac_compute_and_verify_roundtrip` | compute → verify_hmac True |
| 10 | same | `test_hmac_verify_rejects_tampered_body` | body off by 1 byte → False |
| 11 | same | `test_nonce_store_first_add_then_seen` | add(n) → seen(n) True |
| 12 | same | `test_nonce_store_evicts_after_ttl` | monkeypatch monotonic, expire → seen() False |
| 13 | `tests/auth/test_executor_mtls_dep.py` | `test_require_executor_mtls_via_trusted_proxy_header` | `DLW_TLS_TRUSTED_PROXY=1` + `X-Client-Cert-PEM` → returns Executor row |
| 14 | same | `test_require_executor_mtls_rejects_unknown_fingerprint` | header with unregistered cert → 401 |
| 15 | same | `test_require_executor_mtls_rejects_header_when_proxy_disabled` | `DLW_TLS_TRUSTED_PROXY=0` + header → 401 (header ignored) |
| 16 | `tests/auth/test_executor_jwt_dep.py` | `test_require_executor_jwt_accepts_valid_token` | valid JWT + matching mTLS → Executor |
| 17 | same | `test_require_executor_jwt_rejects_sub_mismatch` | JWT `sub` ≠ mTLS executor_id → 401 |
| 17b | `tests/auth/test_executor_epoch.py` | `test_require_executor_epoch_rejects_path_id_mismatch` | mTLS+JWT for executor A, path `/executors/B/...` → 401 EXECUTOR_ID_MISMATCH (confused-deputy guard) |
| 18 | `tests/auth/test_hmac_heartbeat_dep.py` | `test_hmac_heartbeat_accepts_valid_signature` | mTLS + JWT + correct HMAC headers → passes |
| 19 | same | `test_hmac_heartbeat_rejects_clock_skew` | ts off by 400s → 401 CLOCK_SKEW |
| 20 | same | `test_hmac_heartbeat_rejects_replay` | same nonce twice → 2nd is 401 REPLAY_DETECTED |
| 21 | same | `test_hmac_heartbeat_rejects_tampered_body` | sig computed over body A, POST body B → 401 HMAC_INVALID |
| 22 | `tests/api/test_register_endpoint.py` | `test_register_returns_cert_jwt_and_hmac_seed` | enrollment token + CSR → 201 + all 4 fields populated |
| 23 | same | `test_register_rejects_invalid_enrollment_token` | wrong token → 401 |
| 24 | same | `test_register_idempotent_on_reregister` | same executor_id twice → epoch bumped, new fingerprint |
| 25 | `tests/api/test_renew_endpoint.py` | `test_renew_returns_new_jwt_only_when_cert_fresh` | cert TTL > 1h → `client_cert_pem` is null |
| 26 | same | `test_renew_returns_new_cert_when_under_1h` | deliberately short-TTL cert → renew returns a new cert |
| 27 | `tests/e2e/test_executor_auth_e2e.py` | `test_register_then_heartbeat_full_flow` | Real uvicorn TLS subprocess: register → heartbeat 200 with HMAC |

Count ≈ 27. The single e2e (test 27) is the load-bearing wiring check; everything else is module-level.

### 7.2 mTLS test strategy

- **Units (tests 1-26):** bypass real TLS. `tests/conftest.py` gains `ephemeral_ca` (session-scoped — one CA per session) + `client_cert_pair` (per-test client cert signed by that CA) fixtures. Protected-endpoint tests set `DLW_TLS_TRUSTED_PROXY=1` (via `monkeypatch.setenv`) and send `X-Client-Cert-PEM`.
- **e2e (test 27):** spawns uvicorn in a subprocess with real `--ssl-*` flags pointing at the `ephemeral_ca`'s server cert; an httpx client connects with `verify=<ca>` + `cert=<client>`. This is the only test that touches real TLS — it protects the uvicorn wiring + peer-cert extraction path. Slow (~1-2s) but runs once.
- **Why the two layers:** identical rationale to W2a/W2b1's "test pyramid" — module units stay fast (header bypass), one e2e guards the integration seam.

### 7.3 Existing test migration

`/join` deletion forces test-setup migration:

- `tests/api/test_executors.py` — the `joined_executor` fixture → `registered_executor` (build CSR, call `/register`). Heartbeat tests gain HMAC headers (a `_signed_heartbeat_headers(auth_state, body)` helper in conftest).
- `tests/api/test_subtasks.py` — same fixture migration.
- `tests/e2e/test_executor_e2e.py` / `test_happy_path.py` — full flow now starts with `/register`.
- `tests/services/test_executor_service.py` — `join_executor` → `upsert_executor_with_cert` rename; the W1 INSERT-or-bump cases still apply, plus 2 new assertions (cert_fingerprint + hmac_seed written).
- `tests/auth/test_executor_epoch.py` — W1 fence dep unchanged; tests that constructed an executor via `/join` switch to a direct DB insert or `/register`.

Estimated existing-test churn: ~12-15 setups across 5 files. All mechanical (fixture-level), no logic changes.

### 7.4 Test infra

- `cryptography` + `pyjwt[crypto]` — added as runtime deps (§2); `uv sync` picks them up. CI `uv` 0.11.9 handles plain `dependencies` entries fine.
- `ephemeral_ca` / `client_cert_pair` fixtures in `tests/conftest.py` — new, ~30 LOC.
- `_signed_heartbeat_headers` helper — new, computes the 3 HMAC headers for a given body + seed.
- No new pytest plugins. No new CI jobs.

### 7.5 CI 12 checks expectations

| Check | W3a impact |
|---|---|
| pytest | +27 new, ~12-15 modified W1 setups |
| Invariant + cross-ref lint | **+`check_no_bearer_on_executor_routes`** helper |
| OpenAPI lint | `/register` + `/renew` added, `/join` removed, HMAC headers documented |
| Markdown lint | spec/plan cross-ref 04 §2.2 + 02 §4.1 |
| Other 8 | no change |

### 7.6 Not tested

- Real NTP clock sync — `CLOCK_SKEW` tests monkeypatch `time.time`.
- High-concurrency nonce store (>10k) — Phase 3 P-001 baseline.
- CA rotation / forced re-enrollment — Phase 3 ops.
- CRL / cert revocation — Phase 3+.
- The `DLW_TLS_TRUSTED_PROXY=1` production-with-real-proxy path — only the header-bypass logic is unit-tested; a real nginx/Caddy front is operator-territory.

---

## 8. Acceptance Criteria

- [ ] `uv add cryptography pyjwt` — both pinned in `pyproject.toml`; `uv.lock` updated; `uv sync` clean.
- [ ] 1 alembic migration applies clean from W2b2 head (`b1d5ea4944ba`), reverses clean. `executors.hmac_seed_encrypted` exists.
- [ ] `dlw.auth.ca` (incl. `ensure_server_cert` with all 4 SANs) + `jwt_signing` + `hmac_nonce` modules + ~12 unit tests pass.
- [ ] `require_executor_mtls` / `require_executor_jwt` / `require_hmac_heartbeat` deps + ~9 unit tests pass.
- [ ] `POST /register` + `POST /{eid}/renew` endpoints + ~5 API tests pass.
- [ ] One e2e (`test_register_then_heartbeat_full_flow`) exercises real uvicorn TLS and passes.
- [ ] W1 `/join` deleted; `ExecutorJoin` schema deleted; all `/join`-calling test setups migrated.
- [ ] `join_executor` → `upsert_executor_with_cert` rename; writes `cert_fingerprint` + `hmac_seed_encrypted`.
- [ ] Executor side: `cert.py` / `auth_lifecycle.py` new; `client.py` / `runner.py` / `config.py` modified; runner spawns the 3rd background task.
- [ ] `tools/lint_invariants.py` `check_no_bearer_on_executor_routes` reports 0 on production tree.
- [ ] OpenAPI: `/register` + `/renew` documented; `/join` removed; heartbeat HMAC headers added; spectral clean.
- [ ] `docs/operator/` documents `${DLW_CA_DIR}` layout, enrollment-token OOB distribution, uvicorn `--ssl-*` flags, the `DLW_TLS_TRUSTED_PROXY` forgery warning, and host-clock-sync requirement.
- [ ] No new CI jobs. Full suite green: baseline 181 + ~27 new + ~12-15 modified W1 setups.

---

## 9. Implementation Phasing (preview for plan)

The plan will be written by the writing-plans skill after spec approval. Expected milestone shape (4 milestones, ~10 tasks):

- **M1 — Auth substrate.** `uv add` deps + alembic + ORM column + `ca.py` (incl. server cert) + `jwt_signing.py` + `hmac_nonce.py` + ~12 unit tests. No endpoint wiring yet.
- **M2 — Controller deps + endpoints.** 3 FastAPI deps + `/register` + `/renew` endpoints + `main.py` bootstrap + `upsert_executor_with_cert` rename + ~14 unit/API tests. `/join` deleted; W1 test setups migrated.
- **M3 — Endpoint auth migration.** heartbeat/poll/report swap to mTLS+JWT(+HMAC); the e2e test; remaining W1 test-setup migration.
- **M4 — Executor side + lint + OpenAPI + PR.** `cert.py` / `auth_lifecycle.py` / `client.py` / `runner.py` / `config.py` + `check_no_bearer_on_executor_routes` lint + OpenAPI + operator runbook + PR.

Branch: `feat/phase-2-w3a-mtls-jwt-hmac`. Branched off `main` at `ba89a91` (PR #11 merge).

---

## 10. References

- Spec source: brainstormed 2026-05-14 (this document).
- Roadmap: `docs/v2.0/08-mvp-roadmap.md` §2.6 Phase 2 W3 Day 1-3.
- Security: `docs/v2.0/04-security-and-tenancy.md` §2.2 (Executor auth, SEC-01 + SEC-04), §2.2.1-2.2.4.
- Protocol: `docs/v2.0/02-protocol.md` §4.1 (heartbeat HMAC).
- Invariants: `docs/v2.0/INVARIANTS.md` §B (security/tenancy) — INVARIANT 1, 4, 44.
- Predecessor specs:
  - W1: `docs/superpowers/specs/2026-05-11-phase-2-week-1-fence-token-recovery-design.md`
  - W2a: `docs/superpowers/specs/2026-05-13-phase-2-w2a-scheduler-state-machine-design.md`
  - W2b1: `docs/superpowers/specs/2026-05-13-phase-2-w2b1-chunk-level-downloader-design.md`
  - W2b2: `docs/superpowers/specs/2026-05-14-phase-2-w2b2-cancel-and-paused-external-design.md`
- W2b2 PR (merged): https://github.com/l17728/modelpull/pull/11 (squash `ba89a91`).
