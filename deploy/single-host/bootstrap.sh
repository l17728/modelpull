#!/usr/bin/env bash
# modelpull v2.1 single-host bootstrap — idempotent secret generator.
#
# Run BEFORE `docker compose up -d` the first time. Re-running is safe:
# fields already present in .env are kept, only missing ones get
# populated with fresh random values.
#
# Generates:
#   POSTGRES_PASSWORD             — 24-char random
#   MINIO_ROOT_USER               — fixed "minioadmin"
#   MINIO_ROOT_PASSWORD           — 24-char random
#   DLW_SYSTEM_JWT_SECRET         — 32-char random
#   DLW_ADMIN_USERNAME            — fixed "admin"
#   DLW_ADMIN_INITIAL_PASSWORD    — 18-char random (DISPLAYED ONCE)
#   DLW_ENROLLMENT_TOKEN          — 32-char random

set -euo pipefail
cd "$(dirname "$0")"

ENV_FILE=".env"
EXAMPLE_FILE=".env.example"

if [ ! -f "$ENV_FILE" ]; then
  cp "$EXAMPLE_FILE" "$ENV_FILE"
  echo "[bootstrap] created $ENV_FILE from $EXAMPLE_FILE"
fi

_rand() {
  # Generate N random alphanumeric chars (default 24). Uses openssl
  # because the previous `tr -dc ... | head -c N` pattern triggers
  # SIGPIPE under `set -o pipefail` when head closes the pipe after
  # reading N bytes — that surfaced as exit 141 on the first deploy.
  local n="${1:-24}"
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex "$n" | tr -d '\n' | cut -c"1-${n}"
  else
    # Fallback for ultra-minimal images: disable pipefail just for the
    # tr|head pipeline so SIGPIPE on `tr` doesn't kill the whole script.
    ( set +o pipefail
      LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "$n" )
  fi
}

_set_if_placeholder() {
  local key="$1"
  local value="$2"
  local current
  current=$(grep -E "^${key}=" "$ENV_FILE" | head -n1 | cut -d= -f2-)
  case "$current" in
    ""|replace-*|changeme-*)
      # Use a tmp file to avoid sed -i portability issues
      grep -v "^${key}=" "$ENV_FILE" > "$ENV_FILE.tmp"
      echo "${key}=${value}" >> "$ENV_FILE.tmp"
      mv "$ENV_FILE.tmp" "$ENV_FILE"
      echo "[bootstrap] set ${key}"
      ;;
    *)
      echo "[bootstrap] kept existing ${key}"
      ;;
  esac
}

PG_PW=$(_rand 24)
MINIO_PW=$(_rand 24)
JWT_SECRET=$(_rand 32)
ADMIN_PW=$(_rand 18)
ENROLL=$(_rand 32)

_set_if_placeholder POSTGRES_PASSWORD "$PG_PW"
_set_if_placeholder MINIO_ROOT_USER "minioadmin"
_set_if_placeholder MINIO_ROOT_PASSWORD "$MINIO_PW"
_set_if_placeholder DLW_SYSTEM_JWT_SECRET "$JWT_SECRET"
_set_if_placeholder DLW_ADMIN_USERNAME "admin"
_set_if_placeholder DLW_ADMIN_INITIAL_PASSWORD "$ADMIN_PW"
_set_if_placeholder DLW_ENROLLMENT_TOKEN "$ENROLL"

# Permissions — env file holds the admin password + JWT secret.
chmod 600 "$ENV_FILE"

ADMIN_PW_NOW=$(grep '^DLW_ADMIN_INITIAL_PASSWORD=' "$ENV_FILE" | cut -d= -f2-)
echo
echo "==================================================================="
echo "Initial admin credentials — save these now; they're in $ENV_FILE"
echo "  URL:      https://catown.cloud/    (after your reverse proxy points to 127.0.0.1:8001)"
echo "  Username: admin"
echo "  Password: $ADMIN_PW_NOW"
echo
echo "Force a change on first login from the Settings page."
echo "==================================================================="
echo
echo "Next step:  docker compose up -d --build"
