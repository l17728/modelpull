# FU8 — encrypted token-at-rest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: subagent-driven-development / executing-plans. Checkbox steps.

**Goal:** Opt-in encryption of the stored bearer token in `~/.dlw/config.yaml`: when `DLW_CONFIG_KEY` is set, tokens are written as `enc:v1:<salt>:<fernet>` blobs and transparently decrypted on read; when unset, behavior is exactly today's plaintext.

**Spec:** `docs/superpowers/specs/2026-05-22-fu8-token-at-rest-design.md` (read fully — §0 cipher/seams, §1 threat model).

**Locked constraints:**
- Only `cryptography` (already a dep). Fernet + PBKDF2HMAC-SHA256 (200k iters, 16-byte random salt per blob embedded in the value). No new dependency.
- **Backward compatible / opt-in**: every change gated on `DLW_CONFIG_KEY` being set. With it unset, the plaintext path is byte-for-byte unchanged so ALL existing token tests stay green.
- Encrypt at the two write sites (`set_context` token=, `set_config_value` when leaf == `access_token`); decrypt ONLY in `resolve()`. No other read path needs cleartext (config get/list redact; context list shows set|unset; clear_token pops).
- Zero migration/openapi/backend/frontend. CI gate = pytest + `lint_invariants`; `ruff --select I001 --fix`.

---

## File Structure
- **Create** `src/dlw/sdk/_crypto.py` (`is_encrypted`/`encrypt_token`/`decrypt_token`).
- **Modify** `src/dlw/sdk/errors.py` (`TokenDecryptError`).
- **Modify** `src/dlw/sdk/_config.py` (`_config_key`, encrypt in `set_context`+`set_config_value`, decrypt in `resolve`).
- **Modify** `src/dlw/cli/main.py` (`config encrypt` subcommand), `src/dlw/cli/handlers.py` (`_config_cmd` encrypt branch).
- **Create** `tests/sdk/test_crypto.py`, `tests/sdk/test_config_crypto.py`; **extend** `tests/cli/test_cli_config.py`.
- **Modify** `docs/operator/cli-sdk.md`.

---

## Milestone M1 — crypto + config seams

### Task 1: `_crypto.py` + error + config encrypt/decrypt seams
**Files:** new `src/dlw/sdk/_crypto.py`, `src/dlw/sdk/errors.py`, `src/dlw/sdk/_config.py`, new `tests/sdk/test_crypto.py`, new `tests/sdk/test_config_crypto.py`.

- [ ] **Step 1 (failing crypto tests):** `tests/sdk/test_crypto.py`:
  - round-trip: `decrypt_token(encrypt_token("tok", "pw"), "pw") == "tok"`.
  - `is_encrypted(encrypt_token("tok","pw"))` True; `is_encrypted("plain")` False.
  - wrong passphrase: `decrypt_token(blob, "wrong")` raises `TokenDecryptError`.
  - tampered blob: flip a char in the fernet part → `decrypt_token` raises `TokenDecryptError`.
  - non-determinism: `encrypt_token("tok","pw") != encrypt_token("tok","pw")` (random salt).
- [ ] **Step 2: verify FAIL** (module absent).
- [ ] **Step 3 (errors):** add to `src/dlw/sdk/errors.py` (after `Timeout`):
```python
class TokenDecryptError(DlwError):
    """Stored token could not be decrypted (wrong/absent DLW_CONFIG_KEY)."""
```
(No `_ORDER` entry needed — `resolve()` re-raises it as `UsageError`; but if left unmapped `exit_code_for` returns 1 via the `DlwError` fallthrough. To be safe add `(TokenDecryptError, 2)` BEFORE `(DlwError, 1)` in `_ORDER`.)
- [ ] **Step 4 (_crypto.py):**
```python
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
    except (InvalidToken, ValueError, Exception) as e:  # narrow below
        raise TokenDecryptError("cannot decrypt stored token") from e
```
(Refine the except to `(InvalidToken, ValueError, base64.binascii.Error)` — do NOT catch bare `Exception`; the broad clause above is a placeholder, replace it.)
- [ ] **Step 5 (failing config tests):** `tests/sdk/test_config_crypto.py` (use `monkeypatch.setenv("DLW_CONFIG_KEY", "pw")` + a `tmp_path` config; import `set_context`, `set_config_value`, `resolve`, `load_config`):
  - key set: `set_context("dev", server="http://h", token="T1", config_path=p)`; raw `load_config(p)["auth"]["dev"]["access_token"]` startswith `"enc:v1:"` and != `"T1"`; `resolve(server=None, token=None, config_path=p).token == "T1"`.
  - `set_config_value("auth.dev.access_token", "T2", config_path=p)` (key set) → stored encrypted; `resolve(...).token == "T2"`.
  - encrypted on disk, key UNSET (`monkeypatch.delenv`): `resolve(...)` raises `UsageError`.
  - encrypted on disk, WRONG key: `resolve(...)` raises `UsageError`.
  - backward compat: with key UNSET, `set_context("dev2", token="P", config_path=p)` stores plaintext `"P"` (raw on-disk == `"P"`); `resolve(...).token == "P"`.
- [ ] **Step 6: verify FAIL.**
- [ ] **Step 7 (_config.py seams):**
  - Add near the top (after imports): `def _config_key() -> str | None: return os.environ.get("DLW_CONFIG_KEY") or None`. (`os` is already imported.)
  - `set_context` (line ~78) — replace the token-store line:
    ```python
    if token is not None:
        from dlw.sdk._crypto import encrypt_token
        key = _config_key()
        stored = encrypt_token(token, key) if key else token
        cfg.setdefault("auth", {}).setdefault(name, {})["access_token"] = stored
    ```
  - `set_config_value` (line ~123) — before `cur[parts[-1]] = value`, add:
    ```python
        if parts[-1] == "access_token" and value is not None:
            from dlw.sdk._crypto import encrypt_token
            key = _config_key()
            if key and not str(value).startswith("enc:v1:"):
                value = encrypt_token(str(value), key)
    ```
  - `resolve` (line ~181) — after computing `tok`, before the `if not tok` check:
    ```python
        if tok and token is None and os.environ.get("DLW_TOKEN") is None \
                and os.environ.get("DLW_SYSTEM_ADMIN_TOKEN") is None:
            from dlw.sdk._crypto import is_encrypted, decrypt_token
            from dlw.sdk.errors import TokenDecryptError
            if is_encrypted(tok):
                key = _config_key()
                if not key:
                    raise UsageError("stored token is encrypted but "
                                     "DLW_CONFIG_KEY is not set")
                try:
                    tok = decrypt_token(tok, key)
                except TokenDecryptError as e:
                    raise UsageError("cannot decrypt stored token: wrong "
                                     "DLW_CONFIG_KEY?") from e
    ```
    (The guard ensures we only decrypt a value that actually came from the config file — a `--token`/env value is never an `enc:` blob, but the guard is belt-and-suspenders + documents intent. Simpler acceptable variant: just `if tok and is_encrypted(tok):` since flag/env tokens won't have the prefix — implementer may use the simpler form; either is correct.)
- [ ] **Step 8: verify PASS** — `cd "D:/download_weights" && uv run pytest tests/sdk/test_crypto.py tests/sdk/test_config_crypto.py -v` all pass.
- [ ] **Step 9: tidy + commit.**
```bash
cd "D:/download_weights" && uv run ruff check --select I001 --fix src/dlw/sdk/_crypto.py src/dlw/sdk/errors.py src/dlw/sdk/_config.py tests/sdk/test_crypto.py tests/sdk/test_config_crypto.py
git add src/dlw/sdk/_crypto.py src/dlw/sdk/errors.py src/dlw/sdk/_config.py tests/sdk/test_crypto.py tests/sdk/test_config_crypto.py && git commit -m "feat(fu8): opt-in DLW_CONFIG_KEY token encryption at rest (Fernet/PBKDF2)"
```

### Task 2: M1 gate
- [ ] `cd "D:/download_weights" && uv run pytest -q` all pass — esp. the EXISTING token tests (`tests/cli/test_login.py`, `tests/sdk/test_config.py`, `tests/sdk/test_config_write.py`) which do NOT set `DLW_CONFIG_KEY` and must stay green (plaintext path unchanged). (`test_failover_drill.py` = Windows-local flake; isolate-confirm.) `uv run python -m dlw.tools.lint_invariants --strict` OK. No commit.

---

## Milestone M2 — `config encrypt` migrate + docs

### Task 3: migrate command + docs
**Files:** `src/dlw/cli/main.py`, `src/dlw/cli/handlers.py`, `tests/cli/test_cli_config.py`, `docs/operator/cli-sdk.md`.

- [ ] **Step 1 (failing tests):** extend `tests/cli/test_cli_config.py`:
  - `test_config_encrypt_migrates_plaintext`: with `DLW_CONFIG_KEY` UNSET first, write a plaintext token (`config set auth.prod.access_token PLAINTOK` — but note: `config set` with the key unset stores plaintext); then `monkeypatch.setenv("DLW_CONFIG_KEY","pw")` and run `config encrypt` → exit 0, stdout mentions a count ≥1, and raw `load_config(p)["auth"]["prod"]["access_token"]` now startswith `"enc:v1:"`; a follow-up `resolve` (key set) returns `PLAINTOK`. (Build the plaintext seed by setting the env AFTER the `config set`, OR seed the yaml directly via `set_config_value` with the env unset.)
  - `test_config_encrypt_requires_key`: `DLW_CONFIG_KEY` unset → `config encrypt` exits 2 (UsageError).
  - `test_config_encrypt_skips_already_encrypted`: run encrypt twice → second reports 0 migrated (idempotent; already-`enc:` values skipped).
- [ ] **Step 2: verify FAIL.**
- [ ] **Step 3 (parser):** in `cli/main.py` add to the `config` subparser: `cfg_sub.add_parser("encrypt", help="encrypt plaintext stored tokens (needs DLW_CONFIG_KEY)")`.
- [ ] **Step 4 (handler):** in `cli/handlers.py` `_config_cmd`, add a branch:
```python
    if sub == "encrypt":
        import os
        from dlw.sdk._crypto import encrypt_token, is_encrypted
        key = os.environ.get("DLW_CONFIG_KEY") or None
        if not key:
            from dlw.sdk.errors import UsageError
            raise UsageError("config encrypt needs DLW_CONFIG_KEY set")
        cfg = cfgmod.load_config(cp)
        n = 0
        for ctx, entry in (cfg.get("auth") or {}).items():
            tok = entry.get("access_token")
            if isinstance(tok, str) and tok and not is_encrypted(tok):
                entry["access_token"] = encrypt_token(tok, key)
                n += 1
        if n:
            cfgmod.save_config(cfg, config_path=cp)
        if not args.quiet:
            sys.stdout.write(f"encrypted {n} token(s)\n")
        return 0
```
(`UsageError` propagates to `main()` → exit 2.)
- [ ] **Step 5 (docs):** `docs/operator/cli-sdk.md`: document opt-in token encryption — set `DLW_CONFIG_KEY` (a passphrase) and tokens written by `login`/`context set`/`config set` are stored as `enc:v1:` blobs and transparently decrypted on read; `dlw config encrypt` migrates existing plaintext tokens. Document the threat model (real protection only when the key is kept out of the config file / env-supplied; without the key, behavior is unchanged plaintext; does not protect against an attacker with both file AND key). Update the deferral note: FU8 lifts token-at-rest (opt-in env-keyed); OS-keyring (no env needed) + per-context keys / rotation remain follow-ons.
- [ ] **Step 6: verify PASS** — `cd "D:/download_weights" && uv run pytest tests/cli/test_cli_config.py tests/sdk/test_crypto.py tests/sdk/test_config_crypto.py -v` all pass.
- [ ] **Step 7: tidy + commit.**
```bash
cd "D:/download_weights" && uv run ruff check --select I001 --fix src/dlw/cli/main.py src/dlw/cli/handlers.py tests/cli/test_cli_config.py
git add src/dlw/cli/main.py src/dlw/cli/handlers.py tests/cli/test_cli_config.py docs/operator/cli-sdk.md && git commit -m "feat(fu8): dlw config encrypt (migrate plaintext tokens) + docs"
```

### Task 4: M2 full gate
- [ ] `cd "D:/download_weights" && uv run pytest -q` ALL pass (failover flake = Windows-local; isolate-confirm); `lint_invariants --strict` OK. No commit.

---

## Self-Review
- **Spec coverage:** §0 cipher → Task 1 Step 4 ✓; write seams → Step 7 ✓; read seam → Step 7 resolve ✓; `config encrypt` → Task 3 ✓; tests → Tasks 1,3 ✓; §3 milestones → M1/M2 ✓.
- **Placeholder scan:** Task 1 Step 4's `except` clause is explicitly flagged to be narrowed to `(InvalidToken, ValueError, base64.binascii.Error)` — not a TODO, a correctness instruction. Everything else is concrete code.
- **Type consistency:** `encrypt_token(str,str)->str`, `decrypt_token(str,str)->str`, `is_encrypted(str)->bool`; blob `enc:v1:<salt_b64>:<fernet>`; `_config_key()->str|None`. resolve decrypts to plaintext `str`.
- **Open risks for reviewers:** (a) crypto correctness — PBKDF2 200k/SHA256, 16-byte random salt embedded, Fernet authenticated (tamper → InvalidToken)? (b) **backward compat** — with `DLW_CONFIG_KEY` unset, is EVERY path identical to today (existing plaintext tests must stay green)? (c) the `resolve()` decrypt guard — does a `--token`/env value ever get mis-fed to decrypt (it has no `enc:` prefix, so `is_encrypted` is False → never)? (d) `decrypt_token` must NOT catch bare `Exception` (mask bugs) — narrow it. (e) `config encrypt` idempotency (skips `enc:` values) + requires key. (f) is the salt-in-blob design sound (salt not secret; per-value random salt)? (g) does anything LOG the token/blob/key?
