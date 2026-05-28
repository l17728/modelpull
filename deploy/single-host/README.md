# modelpull v2.1 — single-host docker compose deploy

For deploys on a single VM. Two browser-access modes:

  - **Behind your own HTTPS reverse proxy** (recommended) — controller +
    frontend stay loopback-only, your existing nginx/caddy/cloudflared
    terminates TLS at `https://your-domain/`.
  - **Direct port access** (POC / small team) — frontend binds 5173 on
    all interfaces and serves both UI and API via vite-preview's proxy.
    Set `DLW_PREVIEW_ALLOWED_HOSTS=your.domain,1.2.3.4` so vite accepts
    the public Host header.

Stack:

  - PostgreSQL 18
  - MinIO (S3-compatible storage)
  - modelpull controller (FastAPI, `127.0.0.1:8001`, never public)
  - 2 × modelpull executor (no exposed port)
  - Vue frontend (vite preview, binds `0.0.0.0:5173`, proxies `/api`)

## Prerequisites

  - 64-bit Linux VM (Debian/Ubuntu tested; deploy.sh installs docker if
    missing)
  - ≥ 2 GB RAM **plus pre-built frontend dist** (see below) — or ≥ 4 GB
    RAM if you let docker build the SPA on the server
  - ≥ 30 GB disk (download cache + minio volume)
  - Ports 22 (SSH) and 5173 (browser → frontend) — open 5173 in cloud
    firewall if using direct port access mode
  - Optional: an existing HTTPS reverse proxy on the box

## China-region VM checklist

If the VM is in mainland China (or otherwise behind restricted egress),
deploy.sh handles most of it automatically; this is what it does:

  - Detects docker hub unreachability + installs DaoCloud mirror to
    `/etc/docker/daemon.json` (`docker.m.daocloud.io` is the only mirror
    we've found that survives multiple regions; aliyun mirror returns
    HTTP 200 on probe but throttles large pulls to ~2 KB/s, which makes
    `docker compose pull` of minio/postgres time out).
  - Dockerfiles default `PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple/`
    + `APT_MIRROR=mirrors.aliyun.com` — `pnpm` reads
    `NPM_CONFIG_REGISTRY=https://registry.npmmirror.com` from compose env.

## Frontend dist: build locally, ship the artifact

The Vue SPA is ~1.2 MB minified but the rollup build process peaks at
~1 GB of node heap. On a 2 GB VM with the rest of the stack already up
this OOM-kills the build (host runs out of memory). Two options:

**Recommended**: build on your laptop, ship the artifact:

```bash
cd frontend && pnpm install --frozen-lockfile && pnpm build
cd .. && tar czf /tmp/dist.tgz -C frontend dist/
scp /tmp/dist.tgz user@your-vm:/tmp/
ssh user@your-vm "tar xzf /tmp/dist.tgz -C /opt/modelpull/frontend/"
# Now docker compose up — the frontend command sees /app/dist/index.html
# and skips the in-container build.
```

**Fallback**: let the container build. The compose command runs
`NODE_OPTIONS="--max-old-space-size=768" pnpm build` which fits into a
2 GB VM IF you `docker compose stop controller executor-1 executor-2`
first, build, then `docker compose start ...`. Slower (~30s build +
restart dance) but no laptop side-channel.

## Quick start

On your laptop, ship the deploy bundle:

```bash
# from the repo root
tar czf /tmp/modelpull-deploy.tgz \
    pyproject.toml uv.lock alembic.ini \
    src/ frontend/ \
    config/ docs/operator/ MANUAL.md \
    Dockerfile.executor Dockerfile.controller \
    deploy/single-host/

scp /tmp/modelpull-deploy.tgz user@catown.cloud:/tmp/
ssh user@catown.cloud
```

On the VM:

```bash
sudo mkdir -p /opt/modelpull && sudo chown $USER:$USER /opt/modelpull
tar xzf /tmp/modelpull-deploy.tgz -C /opt/modelpull
cd /opt/modelpull/deploy/single-host
sudo bash deploy.sh
```

The script:

  1. Installs docker if missing
  2. Runs `bootstrap.sh` (generates secrets into `.env`, prints the
     initial admin password ONCE)
  3. `docker compose up -d --build`
  4. Waits for the controller to become healthy
  5. Prints next-step instructions

## TLS / reverse proxy

You said your site is already HTTPS, so pick whichever proxy you use
and point it at the loopback ports:

### nginx

Add this server block to your nginx config:

```nginx
server {
    listen 443 ssl http2;
    server_name catown.cloud;

    # your existing cert paths
    ssl_certificate     /etc/letsencrypt/live/catown.cloud/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/catown.cloud/privkey.pem;

    # 1) UI
    location / {
        proxy_pass         http://127.0.0.1:5173/;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Forwarded-Proto https;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
    }

    # 2) Controller API + WebSocket
    location /api/ {
        proxy_pass         http://127.0.0.1:8001;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Forwarded-Proto https;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        # WebSocket upgrade (reverse-WSS endpoint)
        proxy_http_version 1.1;
        proxy_set_header   Upgrade           $http_upgrade;
        proxy_set_header   Connection        "upgrade";
        proxy_read_timeout 3600s;
    }
    location /health/ {
        proxy_pass         http://127.0.0.1:8001/health/;
    }
    location /metrics {
        # Prometheus scrape; restrict by IP if exposing externally
        proxy_pass         http://127.0.0.1:8001/metrics;
    }
}
```

Reload: `sudo nginx -t && sudo systemctl reload nginx`.

### Caddy

Add this site block to your `Caddyfile`:

```caddy
catown.cloud {
    # Caddy auto-handles TLS via letsencrypt
    handle /api/* {
        reverse_proxy 127.0.0.1:8001 {
            header_up Host {host}
            header_up X-Forwarded-Proto https
        }
    }
    handle /health/* {
        reverse_proxy 127.0.0.1:8001
    }
    handle /metrics {
        reverse_proxy 127.0.0.1:8001
    }
    handle {
        reverse_proxy 127.0.0.1:5173
    }
}
```

Reload: `sudo systemctl reload caddy`.

### Cloudflare Tunnel

If you're behind a Cloudflare tunnel, run:

```bash
cloudflared tunnel --hostname catown.cloud --url http://127.0.0.1:5173
```

For the `/api/*` path, route inside the cloudflared config:

```yaml
ingress:
  - hostname: catown.cloud
    path: /api/.*
    service: http://127.0.0.1:8001
  - hostname: catown.cloud
    service: http://127.0.0.1:5173
  - service: http_status:404
```

## Verification

After the reverse proxy is in place:

```bash
# from your laptop
curl https://catown.cloud/health/ready
# expect: {"status":"ready","db":"ok"}
```

Then open `https://catown.cloud/` in a browser, log in with `admin` +
the password printed by `bootstrap.sh`.

## Logs — where to look when something breaks

Two views of the same data:

### A) Persistent files on host (recommended for `grep` / `tail -F`)

The compose file bind-mounts `./logs/` into every container as
`/var/log/dlw`, and each service's `exec ... | tee -a` makes a copy of
its stdout flow land in a stable file. `deploy.sh` `chmod 777`s the
directory so containers (root inside) can write.

**Absolute paths on the VM after `deploy.sh`:**

| Service | File |
|---------|------|
| Controller (FastAPI + dlw services + scheduler) | `/opt/modelpull/deploy/single-host/logs/controller.log` |
| Executor 1 (worker)         | `/opt/modelpull/deploy/single-host/logs/executor-1.log` |
| Executor 2 (worker)         | `/opt/modelpull/deploy/single-host/logs/executor-2.log` |
| Frontend (Vite preview)     | `/opt/modelpull/deploy/single-host/logs/frontend.log` |
| Postgres                    | only docker logs (PG writes to stderr; see below) |
| MinIO                       | only docker logs |

### B) `docker compose logs` (always works, even before file write)

Useful as a fallback when the bind-mount file isn't there yet (first 30s
after start) or when you want timestamps from the docker daemon:

```bash
cd /opt/modelpull/deploy/single-host
docker compose logs -f controller        # tail controller live
docker compose logs --tail 200 executor-1  # last 200 lines
docker compose logs                       # all services, all time
```

The `json-file` driver caps each container at **7 × 100MB = 700 MB**
(rotation built into docker), so disk can't fill from logs even on a
long-running deploy.

### Helper: `logs.sh`

```bash
cd /opt/modelpull/deploy/single-host
bash logs.sh paths                # print every log path + size
bash logs.sh tail                 # tail -F controller.log
bash logs.sh tail executor-1      # tail one executor
bash logs.sh tail-all             # interleaved tail of controller + both executors
bash logs.sh errors               # grep WARNING/ERROR/Traceback from last hour
bash logs.sh errors 24            # ... from last 24 hours
bash logs.sh snapshot             # tarball of everything for bug reports
bash logs.sh rotate               # force a fresh log file
```

### Turn on DEBUG (verbose)

For deep diagnostics, edit `.env` and add or change:

```ini
DLW_LOG_LEVEL=DEBUG
```

Then `docker compose up -d` re-applies. This propagates to:
  - controller python logger (`logger.debug(...)` lines appear)
  - both executor `--log-level DEBUG`

Remember to flip back to `INFO` after — DEBUG can quadruple log volume.

### Common grep patterns

```bash
# All v2.1 SLA tier admission rejections
grep "admission_denied_" logs/controller.log

# Replication job failures
grep "replication.*failed\|replication.*error" logs/controller.log

# Reverse-WSS connection issues
grep "reverse_ws" logs/controller.log | grep -v "heartbeat"

# Executor task failures (cross-executor)
grep -E "failed|error|Exception" logs/executor-*.log

# Admin role-denied attempts (security review)
grep "role_denied" logs/controller.log
```

## Day-2 operations

| Action | Command (run in `deploy/single-host/`) |
|--------|----------------------------------------|
| Tail controller logs | `bash logs.sh tail` |
| Tail one executor    | `bash logs.sh tail executor-1` |
| Errors last hour     | `bash logs.sh errors` |
| Restart controller   | `docker compose restart controller` |
| Apply a code update  | `git pull && bash deploy.sh --rebuild` |
| Stop everything      | `docker compose down` (keeps volumes) |
| Wipe everything      | `docker compose down -v` (DELETES data!) |
| Check disk usage     | `docker system df && du -sh logs/` |
| Open a PG shell      | `docker compose exec postgres psql -U postgres dlw` |
| Open MinIO UI        | SSH-tunnel `127.0.0.1:9001` or reverse-proxy a `/minio/` subpath |
| Bug-report snapshot  | `bash logs.sh snapshot` (creates a `.tgz` to attach) |

## Upgrades

The deploy bundle is self-contained. To upgrade:

  1. On your laptop, pull `main` and rebuild the tarball as above
  2. scp + extract overwriting `/opt/modelpull/`
  3. `cd /opt/modelpull/deploy/single-host && sudo bash deploy.sh --rebuild`

`deploy.sh --rebuild` re-runs `alembic upgrade head` via the `migrate`
container before flipping over the controller, so schema migrations
ship atomically.

## Migrating to another host

`migrate.sh` moves the controller (with its PostgreSQL + MinIO data + secrets
+ mTLS CA) and/or the executors to a new VM. Three verbs:

| Verb | Runs on | What it does |
|------|---------|--------------|
| `export`   | source | Snapshots the chosen component into a `.tgz` bundle (source left fully intact) |
| `import`   | target | Restores a bundle and brings the stack up; **refuses to clobber** existing target data unless `--force` |
| `transfer` | source | One-click: `export` → `scp` to target → `import` over SSH (source needs SSH access to target) |

`--component` selects what moves:

- **`controller`** — `pg_dump` of the `dlw` DB + MinIO objects + the CA volume
  + `.env` secrets + the build context. A faithful relocation: tasks, tenants,
  executor registrations, downloaded objects all come across.
- **`executor`** — produces a *standalone* executor bundle pre-pointed at a
  controller URL you supply, for standing up a worker on a new box.
- **`all`** — controller + its co-located 2-executor stack (a full move).

### One-click examples

```bash
# Full move of everything to a new VM (run on the source):
bash migrate.sh transfer --component all --target root@new-vm --yes

# Relocate just the controller (keeps all data), then repoint executors:
bash migrate.sh transfer --component controller --target root@new-vm --yes
#   then on each executor host:
bash migrate.sh repoint-executors --controller-url https://new-domain/

# Add an executor on a new worker host:
bash migrate.sh transfer --component executor --target root@worker2 \
     --controller-url https://catown.cloud/ --s3-url http://10.0.0.5:9000
```

Manual (no source→target SSH): `export` here, copy the `.tgz` yourself, then
`import --in <bundle>` on the target.

### Prerequisites & notes

- **Remote executors must be able to reach the controller over the network.**
  The controller binds `127.0.0.1:8001` only, so a worker on another host
  reaches it via your HTTPS reverse proxy — pass `--controller-url
  https://your-domain/` (the proxy forwards `/api/*` to the controller). The
  `--s3-url` must point at a MinIO/S3 endpoint the worker can reach for uploads.
- **Give each new executor a unique id.** The generated executor bundle's
  `.env` has placeholder `DLW_EXECUTOR_ID` / `DLW_EXECUTOR_HOST_ID` — edit them
  before first start so registrations don't collide.
- **Consistency**: `export` briefly stops the controller + executors so the PG
  dump and MinIO copy are coherent, then restarts them. Use `--dry-run` to
  preview every step, `--yes` to skip prompts, `--include-images` to ship
  `docker save`d images (skips the slow rebuild on the target — handy given the
  `uv sync` slowness on China-region VMs, see above).
- **After a controller move**: point DNS / your reverse proxy at the new host,
  then `repoint-executors --controller-url <new-url>` on each executor host.

## Sizing notes

| Field | Default | When to bump |
|-------|---------|--------------|
| Executor count | 2 | More executors = more parallel downloads. Add `executor-3:` block in `docker-compose.yml`. |
| Executor concurrency | 4 | `DLW_EXECUTOR_CHUNK_CONCURRENCY` env on each executor. |
| Controller workers | 1 (uvicorn default) | Single-host doesn't need more; for active/standby use the helm chart. |
| MinIO disk | docker volume | For > 100 GB models, mount a host path: replace `dlw-miniodata` with `/srv/minio:/data`. |

## Security

  - `.env` has `chmod 600` after `bootstrap.sh` runs — only root +
    your user can read it. Don't commit it.
  - The controller binds to `127.0.0.1` only — the docker-published
    port is unreachable from the public network even without a firewall.
  - PG and MinIO have **no** host port published; only the docker
    network sees them.
  - Reverse-proxy `/api/*` and `/health/`, NOT `/metrics`, to the
    public internet — Prometheus scrape should come from inside the box.

## What's NOT included

  - Active/standby controller HA — use the helm chart (`deploy/helm/`)
    for that. Single-host is single-controller by design.
  - Automatic TLS — you said your site is already HTTPS; we don't
    duplicate that.
  - Monitoring stack — Prometheus + Grafana have their own deploys
    under `deploy/prometheus/` and `deploy/grafana/`. Single-host can
    add them later as additional compose services.
  - Backups — `docker compose exec postgres pg_dump -U postgres dlw |
    gzip > /backup/dlw-$(date +%F).sql.gz` is the simplest path; cron
    it daily on the host.

## Troubleshooting

| Symptom | First thing to check |
|---------|---------------------|
| `bash deploy.sh` fails on `docker compose build` | Check disk space (`df -h`) and that the source tar landed correctly under `/opt/modelpull/` |
| Controller `/health/ready` 503 | `docker compose logs controller` — usually PG migration didn't run; rerun `docker compose run --rm migrate` |
| Executor not registering | `docker compose logs executor-1` — most common cause is `DLW_ENROLLMENT_TOKEN` mismatch. Run `bootstrap.sh` again to confirm token. |
| Browser can't reach UI | Reverse-proxy not configured or wrong port. `curl http://127.0.0.1:5173/` on the box should return HTML. |
| Login returns 500 | Empty `DLW_ADMIN_INITIAL_PASSWORD` in `.env` — re-run `bootstrap.sh` and `docker compose restart controller`. |
