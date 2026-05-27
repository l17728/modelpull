"""One-shot deploy script — driven from operator's laptop.

Uses paramiko (already pulled via `uv run --with paramiko`) so we don't
need sshpass / interactive prompts. Reads credentials from env vars:

  DEPLOY_HOST       — e.g. catown.cloud
  DEPLOY_USER       — e.g. root
  DEPLOY_PASSWORD   — SSH password (NOT logged)
  DEPLOY_TARBALL    — local path to the .tgz to ship
  DEPLOY_REMOTE_DIR — remote install root (default /opt/modelpull)

Flow:
  1. Connect over SSH (paramiko Transport)
  2. SFTP the tarball to /tmp/
  3. Exec: mkdir -p REMOTE_DIR && tar xzf /tmp/...tgz -C REMOTE_DIR
  4. Exec: cd REMOTE_DIR/deploy/single-host && bash deploy.sh
  5. Stream every line of stdout/stderr back to the operator console

Idempotent — re-running overwrites the deploy dir + re-runs deploy.sh,
which is itself idempotent (bootstrap.sh keeps existing .env values,
docker compose up only restarts changed services)."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import paramiko

# Windows default console is GBK; docker buildkit emits braille (U+2800
# range) progress chars that GBK can't encode → UnicodeEncodeError mid-
# stream killed the first deploy attempt. Force UTF-8 + errors=replace
# so unrepresentable bytes turn into '?' instead of bringing the script
# down with the remote bash session.
for stream_name in ("stdout", "stderr"):
    stream = getattr(sys, stream_name)
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    v = os.environ.get(name, default)
    if required and not v:
        sys.stderr.write(f"[deploy] missing env var: {name}\n")
        sys.exit(2)
    return v or ""


def _connect(host: str, user: str, password: str, port: int = 22) -> paramiko.SSHClient:
    print(f"[deploy] connecting to {user}@{host}:{port}")
    client = paramiko.SSHClient()
    # Accept first-seen host key — this is an interactive deploy from a
    # known operator, not a long-lived service.
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host, port=port, username=user, password=password,
        timeout=20, banner_timeout=20, auth_timeout=20,
        allow_agent=False, look_for_keys=False,
    )
    print(f"[deploy] connected")
    return client


def _upload(client: paramiko.SSHClient, local_path: Path, remote_path: str) -> None:
    size = local_path.stat().st_size
    print(f"[deploy] uploading {local_path.name} ({size/1024:.0f} KB) → {remote_path}")
    sftp = client.open_sftp()
    t0 = time.monotonic()
    last_pct = -1

    def _progress(transferred: int, total: int) -> None:
        nonlocal last_pct
        pct = int(transferred * 100 / max(total, 1))
        if pct != last_pct and pct % 10 == 0:
            print(f"[deploy]   {pct}% ({transferred//1024} KB)")
            last_pct = pct

    try:
        sftp.put(str(local_path), remote_path, callback=_progress)
    finally:
        sftp.close()
    print(f"[deploy] upload done in {time.monotonic()-t0:.1f}s")


def _exec_streaming(client: paramiko.SSHClient, command: str,
                     *, timeout_s: float = 1800) -> int:
    print(f"[deploy] $ {command}")
    # get_pty so deploy.sh's tty-aware programs (docker buildx) work
    stdin, stdout, stderr = client.exec_command(command, get_pty=True,
                                                  timeout=timeout_s)
    channel = stdout.channel

    deadline = time.monotonic() + timeout_s
    last_activity = time.monotonic()
    while True:
        if channel.recv_ready():
            data = channel.recv(65536).decode("utf-8", errors="replace")
            if data:
                # Strip ANSI cursor codes that look ugly relayed
                sys.stdout.write(data)
                sys.stdout.flush()
                last_activity = time.monotonic()
        if channel.recv_stderr_ready():
            data = channel.recv_stderr(65536).decode("utf-8", errors="replace")
            if data:
                sys.stderr.write(data)
                sys.stderr.flush()
                last_activity = time.monotonic()
        if channel.exit_status_ready() and not (
                channel.recv_ready() or channel.recv_stderr_ready()):
            break
        if time.monotonic() > deadline:
            channel.close()
            raise TimeoutError(
                f"command exceeded {timeout_s}s wall-clock budget")
        # Quiet-channel guard: if 15 min go by with zero bytes, something
        # is wedged. Better to error than hang the deploy.
        if time.monotonic() - last_activity > 900:
            channel.close()
            raise TimeoutError("command was silent for 15 minutes; aborting")
        time.sleep(0.1)

    rc = channel.recv_exit_status()
    print(f"[deploy] (exit {rc})")
    return rc


def main() -> int:
    host = _env("DEPLOY_HOST", required=True)
    user = _env("DEPLOY_USER", "root")
    password = _env("DEPLOY_PASSWORD", required=True)
    tarball = Path(_env("DEPLOY_TARBALL", required=True))
    remote_dir = _env("DEPLOY_REMOTE_DIR", "/opt/modelpull")
    port = int(_env("DEPLOY_PORT", "22"))

    if not tarball.exists():
        sys.stderr.write(f"[deploy] tarball not found: {tarball}\n")
        return 2

    remote_tar = f"/tmp/{tarball.name}"

    client = _connect(host, user, password, port=port)
    try:
        _upload(client, tarball, remote_tar)

        # Step A — extract
        rc = _exec_streaming(
            client,
            f"set -e; mkdir -p {remote_dir} && "
            f"tar xzf {remote_tar} -C {remote_dir} && "
            f"echo '[deploy] extracted'",
            timeout_s=300)
        if rc != 0:
            sys.stderr.write(f"[deploy] extract failed with rc={rc}\n")
            return rc

        # Step B — run the deploy.sh on the box. This includes apt
        # install docker if missing, bootstrap, compose up, healthcheck
        # wait. May take 5-15 minutes the first time.
        # BUILDKIT_PROGRESS=plain → no braille spinner, line-oriented
        # output that streams cleanly over SSH and is greppable.
        # DOCKER_CLI_HINTS=false → suppress upsell lines.
        rc = _exec_streaming(
            client,
            f"cd {remote_dir}/deploy/single-host && "
            f"chmod +x deploy.sh bootstrap.sh logs.sh && "
            f"BUILDKIT_PROGRESS=plain DOCKER_CLI_HINTS=false "
            f"bash deploy.sh",
            timeout_s=1800)
        if rc != 0:
            sys.stderr.write(f"[deploy] deploy.sh failed with rc={rc}\n")
            return rc

        # Step C — show final state
        _exec_streaming(
            client,
            f"cd {remote_dir}/deploy/single-host && "
            f"docker compose ps && echo '---' && "
            f"bash logs.sh paths",
            timeout_s=60)

        print(f"\n[deploy] ✅ deployment complete on {host}")
        print(f"[deploy] next: configure your HTTPS reverse proxy")
        print(f"[deploy]       (see {remote_dir}/deploy/single-host/README.md "
              "§ TLS / reverse proxy)")
        return 0
    finally:
        client.close()
        print("[deploy] ssh connection closed")


if __name__ == "__main__":
    sys.exit(main())
