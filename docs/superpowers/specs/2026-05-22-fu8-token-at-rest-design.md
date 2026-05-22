# FU8 — encrypted token-at-rest for `~/.dlw/config.yaml`

## Problem

`dlw login` / `context set --token` / `config set auth.<ctx>.access_token` all
write the bearer token as **plaintext** YAML (chmod 600 best-effort, a **no-op on
Windows**). The deferral note (`docs/operator/cli-sdk.md`):

> **Secure token-at-rest (FU8)** is deferred — tokens remain plaintext,
> chmod-600 best-effort (no-op on Windows).

FU8 lets the token be encrypted at rest.

## §0 Design — opt-in, env-keyed, backward-compatible

### Why env-keyed (not machine-bound, not config-stored)

Real at-rest encryption needs a key that does **not** live next to the
ciphertext. Storing the key in the same config file is pointless; deriving it
from machine info (hostname/user) is obfuscation, not security, and would give a
false sense of protection. The only legitimate non-interactive key source
available is an **environment variable the operator supplies** (from their shell
profile, a secrets manager, CI secret, etc.). The repo has only `cryptography`
(no `keyring`); OS-keychain integration (key lives in the OS keystore, no env
needed) is a documented follow-on that would add the `keyring` dependency.

So FU8 is **opt-in**: if `DLW_CONFIG_KEY` is set, tokens are stored encrypted and
transparently decrypted on read; if it is unset, behavior is **exactly today's**
(plaintext). This guarantees zero breakage of existing flows/tests and lets users
who want at-rest protection get real encryption.

### Cipher (`src/dlw/sdk/_crypto.py`, new — uses only `cryptography`)

- Key derivation: `PBKDF2HMAC(SHA256, length=32, salt=<16 random bytes>,
  iterations=200_000)` over `DLW_CONFIG_KEY.encode()` → `urlsafe_b64encode` →
  `Fernet(key)`. (Fernet = AES-128-CBC + HMAC-SHA256, authenticated.)
- On-disk blob format (single opaque string): `enc:v1:<b64(salt)>:<fernet_token>`.
  The salt is **not** secret; embedding it per-value makes each blob
  self-contained (re-derive the key on decrypt). Fernet tokens and urlsafe-b64
  salt contain no `:`, so `split(":", 3)` parses cleanly.
- API:
  - `is_encrypted(value: str) -> bool` — `value.startswith("enc:v1:")`.
  - `encrypt_token(plaintext: str, passphrase: str) -> str` — random salt →
    blob.
  - `decrypt_token(blob: str, passphrase: str) -> str` — parse salt, re-derive,
    `Fernet(...).decrypt(...)`; raises `TokenDecryptError` (a new sdk error) on a
    wrong key / tampered blob.

### `_config.py` seams (the only behavioral change)

A private `_config_key() -> str | None = os.environ.get("DLW_CONFIG_KEY") or None`.

- **Encrypt-on-write** (two token write sites):
  - `set_context(..., token=...)`: if `token is not None` and `_config_key()` set,
    store `encrypt_token(token, key)`; else store the plaintext (today's
    behavior).
  - `set_config_value(key, value, ...)`: if the dotted key's leaf is
    `access_token`, `value is not None`, and `_config_key()` set, encrypt before
    storing. (Routes the `config set auth.<ctx>.access_token` path through
    encryption too.)
- **Decrypt-on-read** (the single read seam): `resolve()` line ~181 wraps the
  config-sourced token: after `tok = (... or auth.get("access_token"))`, if `tok`
  and `is_encrypted(tok)`: require `_config_key()` (else raise `UsageError(
  "stored token is encrypted but DLW_CONFIG_KEY is not set")`) and
  `tok = decrypt_token(tok, key)` (a `TokenDecryptError` → `UsageError(
  "cannot decrypt stored token: wrong DLW_CONFIG_KEY?")`). The `--token`/env
  precedence is unchanged — decryption only applies when the token actually comes
  from the config file.

No other read path needs decryption: `config get`/`list` already redact the
`access_token` leaf to `***` (FU7), `context list/current` show only `set|unset`,
`clear_token` just pops the key. So the encrypted blob is never displayed and the
only consumer that needs the cleartext is `resolve()`.

### CLI — `dlw config encrypt` (migrate plaintext → encrypted)

A new `config encrypt` subcommand re-writes any existing **plaintext**
`auth.*.access_token` values as encrypted blobs (skips already-encrypted ones).
Requires `DLW_CONFIG_KEY` (else `UsageError`). Prints the count migrated. This is
the one-shot upgrade path for users who already have plaintext tokens on disk.
(`config set`/`login`/`context set` encrypt going forward automatically when the
key is set; `config encrypt` handles the pre-existing ones.)

### Explicitly NOT in scope (documented deferrals)

- **OS keyring** (key in the OS keychain, no env var) — needs the `keyring` dep;
  named follow-on.
- **Per-context keys / key rotation command** — one `DLW_CONFIG_KEY` for all
  contexts; rotation = `config encrypt` after changing the key is NOT automatic
  (you'd need the old key to decrypt first). Documented limitation.
- **Encrypting non-token config** (server URLs etc.) — only `access_token` leaves
  are encrypted; the rest stays plaintext (it isn't secret).

## §1 Threat model (documented honestly)

- WITH `DLW_CONFIG_KEY` set and kept out of the config file: the token is
  AES-encrypted at rest — protects against casual disk inspection, accidental
  commit of `config.yaml`, backups, and (on Windows, where chmod is a no-op)
  other-user reads. The key's secrecy is the operator's responsibility.
- WITHOUT the key: identical to today (plaintext). FU8 adds capability, it does
  not change the default.
- It does NOT protect against an attacker who has BOTH the config file AND the
  env/process that holds `DLW_CONFIG_KEY` (same as any env-keyed scheme).

## §2 Tests

`tests/sdk/test_crypto.py`:
- `encrypt_token`→`decrypt_token` round-trip; `is_encrypted` true for the blob,
  false for plaintext; wrong passphrase → `TokenDecryptError`; tampered blob →
  `TokenDecryptError`; two encryptions of the same plaintext differ (random salt).

`tests/sdk/test_config_crypto.py` (or extend `tests/sdk/test_config_write.py`):
- with `DLW_CONFIG_KEY` set: `set_context("dev", token="T1")` writes a blob whose
  on-disk `auth.dev.access_token` `startswith("enc:v1:")` (NOT `"T1"`); `resolve()`
  returns `"T1"`.
- `set_config_value("auth.dev.access_token", "T2")` with key set → stored
  encrypted; `resolve()` → `"T2"`.
- encrypted token on disk + `DLW_CONFIG_KEY` UNSET → `resolve()` raises
  `UsageError`.
- encrypted token + WRONG key → `resolve()` raises `UsageError`.
- **backward compat**: WITHOUT the key, `set_context(token="T")` stores plaintext
  `"T"` and `resolve()` returns `"T"` (assert the existing plaintext tests still
  hold — they don't set the env).

`tests/cli/test_cli_config.py` (extend): `config encrypt` with `DLW_CONFIG_KEY`
set migrates a pre-seeded plaintext token (on-disk value flips to `enc:v1:`),
prints a count; without the key → exit 2.

## §3 Milestones

- **M1** — `_crypto.py` + `TokenDecryptError` + `_config.py` encrypt/decrypt seams
  + crypto/config tests. Gate: `pytest tests/sdk -q` + the existing token tests
  green (no env key set in them).
- **M2** — `dlw config encrypt` migrate command + docs + full gate. Gate: full
  `pytest -q`, `lint_invariants --strict`.

## §4 Notes

- Zero migration / openapi / backend / frontend. Only `src/dlw/sdk/_crypto.py`
  (new), `src/dlw/sdk/_config.py`, `src/dlw/sdk/errors.py` (new error),
  `src/dlw/cli/main.py`, `src/dlw/cli/handlers.py`, tests, docs.
- Backward compatibility is the linchpin: every change is gated on
  `DLW_CONFIG_KEY` being set, so the default plaintext path (and all existing
  tests that assert plaintext on disk) is byte-for-byte unchanged.
