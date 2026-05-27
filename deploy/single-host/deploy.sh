#!/usr/bin/env bash
# modelpull v2.1 single-host one-shot deploy — runs on the target VM.
#
# Use:  cd /opt/modelpull/deploy/single-host && bash deploy.sh
#
# Idempotent: re-running is a noop if everything is already up + healthy.
# To rebuild after a code change: bash deploy.sh --rebuild

set -euo pipefail
cd "$(dirname "$0")"

REBUILD=0
[ "${1:-}" = "--rebuild" ] && REBUILD=1

echo "[deploy] step 1: docker availability check"
if ! command -v docker >/dev/null 2>&1; then
  echo "[deploy] docker not installed; installing (Debian/Ubuntu)"
  if [ "$(id -u)" -ne 0 ]; then
    echo "[deploy] need root for apt install — re-run as: sudo bash deploy.sh"
    exit 1
  fi
  apt-get update
  apt-get install -y ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  OS_ID=$(. /etc/os-release && echo "$ID")
  OS_CODENAME=$(. /etc/os-release && echo "${VERSION_CODENAME:-bookworm}")
  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
    https://download.docker.com/linux/$OS_ID $OS_CODENAME stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
  systemctl enable --now docker
fi
docker --version
docker compose version

echo "[deploy] step 2: bootstrap secrets (idempotent)"
bash bootstrap.sh

echo "[deploy] step 2b: prepare host log directory"
# The compose file bind-mounts ./logs into every container as /var/log/dlw.
# Pre-create it world-writable enough that root-in-container can append
# (the containers don't run as the host user). 770 + owner adjustment is
# the security-aware path; 777 is the get-it-working path for a one-host
# deploy where the box already trusts whoever ran deploy.sh.
mkdir -p logs
chmod 777 logs  # tradeoff: containers run as root so write needs g/o write

echo "[deploy] step 3: build + bring up the stack"
if [ "$REBUILD" -eq 1 ]; then
  docker compose build --pull
fi
docker compose up -d --build

echo "[deploy] step 4: wait for controller /healthz to return 200"
ATTEMPTS=0
until docker compose exec -T controller curl -fsS http://localhost:8001/healthz >/dev/null 2>&1; do
  ATTEMPTS=$((ATTEMPTS+1))
  if [ "$ATTEMPTS" -gt 60 ]; then
    echo "[deploy] controller didn't become healthy in 5 min; check logs:"
    echo "  docker compose logs --tail 200 controller"
    exit 1
  fi
  sleep 5
done

echo "[deploy] step 5: verify executors registered"
sleep 8  # let the executors join after controller becomes healthy
docker compose ps

echo
echo "==================================================================="
echo "modelpull v2.1 is up."
echo
echo "  Controller (loopback):  http://127.0.0.1:8001/healthz"
echo "  Frontend (loopback):    http://127.0.0.1:5173/"
echo "  MinIO console:          http://127.0.0.1:9001/"
echo
echo "Persistent log files on host (also via 'docker compose logs'):"
LOGS_ABS=$(realpath logs)
echo "  $LOGS_ABS/controller.log    (FastAPI + dlw service log)"
echo "  $LOGS_ABS/executor-1.log    (worker 1)"
echo "  $LOGS_ABS/executor-2.log    (worker 2)"
echo "  $LOGS_ABS/frontend.log      (Vue preview)"
echo "  $LOGS_ABS/postgres.log      (only if PG is configured to write here)"
echo
echo "Quick tail commands:"
echo "  tail -F $LOGS_ABS/controller.log"
echo "  bash $(pwd)/logs.sh tail controller   # same thing, shorter"
echo "  bash $(pwd)/logs.sh errors            # grep last hour of errors"
echo
echo "Now point your existing HTTPS reverse proxy at the loopback ports."
echo "See README.md § 'TLS / reverse proxy' for nginx + caddy templates."
echo
echo "Admin credentials are in .env (DLW_ADMIN_INITIAL_PASSWORD)."
echo "==================================================================="
