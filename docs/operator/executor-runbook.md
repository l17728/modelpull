# Executor Runbook

## `.parts/` staging area (Phase 2 W2b1+)

Executors that handle files ≥ 100 MiB stage downloads into
`${DLW_EXECUTOR_PARTS_DIR_PATH}/` (default `./parts`) before uploading
to S3. In production:

- Mount a writable PV at the configured path; sized to at least the
  largest expected single file + 20% headroom.
- Operator must `chown` the dir to the user running the executor
  process.
- Controller's `sweep_paused_disk_full` recovers subtasks back to
  `pending` if disk frees up. No manual intervention needed for
  transient ENOSPC.

## Task cancellation latency (Phase 2 W2b2+)

`POST /api/v1/tasks/{task_id}/cancel` flips the task to `cancelling`. The
scheduler stops handing out new subtasks for that task immediately.
In-flight subtasks finish naturally:

- Small files (< 100 MiB, W4 `HfS3StreamDownloader`): typically seconds.
- Large files (>= 100 MiB, W2b1 `DirectOffsetDownloader`): can take **up to
  several minutes** depending on file size and bandwidth.

The task stays in `cancelling` until the last in-flight subtask reaches a
terminal state, then transitions to `cancelled`. Paused subtasks
(`paused_disk_full` / `paused_external`) at the moment of `/cancel` are
force-terminated synchronously inside the cancel transaction.

If a task stays in `cancelling` for unexpectedly long (e.g. > 30 minutes
on a fast network), check executor logs for stuck downloads. Operator
escalation: re-issue `/cancel` — it is idempotent and will re-force-terminate
any paused subtasks that appeared after the original cancel.

A future Phase 2 W3 release will add heartbeat-carried cancellation
signals so executors abort in-flight downloads on chunk boundaries,
reducing latency to sub-minute.

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
      --ssl-cert-reqs 1

`--ssl-cert-reqs 1` (CERT_OPTIONAL) — the server requests a client cert but
does not reject connections that lack one at the TLS layer. `/register`
(enrollment-token auth, no client cert) and `/health/*` need this. The
application layer (`require_executor_mtls`) enforces the cert where required.

### Enrolling an executor

1. Copy the controller's enrollment token to the executor host out-of-band.
2. Set `DLW_EXECUTOR_ENROLLMENT_TOKEN` on the executor.
3. On first boot the executor generates a keypair, builds a CSR, calls
   `/register`, and persists `client-cert.pem` / `client-key.pem` /
   `ca-chain.pem` / `hmac-seed` under `${DLW_EXECUTOR_EXECUTOR_CERT_DIR}`
   (default `./.executor-certs`).
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

## W3b — HF access via the controller reverse proxy

As of Phase 2 W3b, executors no longer talk to huggingface.co directly. All
HF file downloads flow through the controller's reverse proxy
(`GET /api/v1/hf-proxy/subtask/{id}`), which injects the tenant HF token
server-side (INVARIANT 2 — the token never leaves the controller).

**Removed executor environment variables** (delete them from `.env.executor`
and any deployment manifests — they are now ignored):

- `DLW_EXECUTOR_HF_TOKEN`
- `DLW_EXECUTOR_HF_ENDPOINT`

**Controller environment variables:**

- `DLW_HF_TOKEN` — the tenant HF token (already used by the controller for
  repo-metadata enumeration; W3b also uses it for the download proxy).
- `DLW_HF_ENDPOINT` — defaults to `https://huggingface.co`.
- `DLW_HF_PROXY_TIMEOUT_SECONDS` — per-request timeout for the proxy's HF
  fetch (default 300, range 10–3600).

**Operational tradeoff:** download bandwidth now flows through the controller
rather than executor→HF directly. For the internal beta this is acceptable;
global rate-limit coordination and an executor-local credential pool are
Phase 3 items.
