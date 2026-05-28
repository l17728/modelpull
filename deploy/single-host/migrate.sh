#!/usr/bin/env bash
# modelpull v2.1 — one-click host migration.
#
# Move the controller (with its PostgreSQL + MinIO data + secrets + CA) and/or
# the download executors from one VM to another. Three verbs:
#
#   export    — on the SOURCE host: snapshot the chosen component into a tarball
#   import    — on the TARGET host: restore a tarball and bring the stack up
#   transfer  — one-click: export here → scp to target → import there (source
#               needs SSH access to the target)
#
# Component selector (--component):
#   controller   PG dump + MinIO objects + CA + .env + deploy files (+ images)
#   executor     a self-contained executor-only bundle for a NEW worker host,
#                pre-pointed at the controller URL you give
#   all          controller + a co-located 2-executor stack (a full move)
#
# Examples:
#   # Full move of everything to a new box, one command (run on the source):
#   bash migrate.sh transfer --component all --target root@new-vm
#
#   # Just relocate the controller (keep data); executors get repointed after:
#   bash migrate.sh transfer --component controller --target root@new-vm
#   #   then on each executor host:
#   bash migrate.sh repoint-executors --controller-url https://new-domain/
#
#   # Stand up an extra executor on a new worker box:
#   bash migrate.sh export --component executor \
#        --controller-url https://catown.cloud/ --s3-url http://10.0.0.5:9000
#   scp _migration/modelpull-executor-*.tgz worker2:/tmp/
#   ssh worker2 'mkdir -p /opt/modelpull-exec && tar xzf /tmp/modelpull-executor-*.tgz -C /opt/modelpull-exec && cd /opt/modelpull-exec && bash run-executor.sh'
#
# Manual (no source→target SSH): run `export` here, copy the tarball yourself,
# run `import --in <tarball>` on the target.
#
# SAFETY: export quiesces writers (stops controller+executors) so the PG dump
# and MinIO copy are consistent, then restarts them. import REFUSES to clobber
# a target that already has data unless --force. Nothing is deleted on the
# source. Use --dry-run to preview, --yes to skip prompts.

set -euo pipefail
cd "$(dirname "$0")"

# --------------------------------------------------------------------------
# defaults + arg parsing
VERB="${1:-}"; shift || true
COMPONENT=""
OUT_DIR="./_migration"
IN_BUNDLE=""
TARGET=""
TARGET_DIR="/opt/modelpull"
CONTROLLER_URL=""
S3_URL=""
INCLUDE_IMAGES=0
ASSUME_YES=0
DRY_RUN=0
FORCE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --component)      COMPONENT="$2"; shift 2 ;;
    --out)            OUT_DIR="$2"; shift 2 ;;
    --in)             IN_BUNDLE="$2"; shift 2 ;;
    --target)         TARGET="$2"; shift 2 ;;
    --target-dir)     TARGET_DIR="$2"; shift 2 ;;
    --controller-url) CONTROLLER_URL="$2"; shift 2 ;;
    --s3-url)         S3_URL="$2"; shift 2 ;;
    --include-images) INCLUDE_IMAGES=1; shift ;;
    --yes|-y)         ASSUME_YES=1; shift ;;
    --dry-run)        DRY_RUN=1; shift ;;
    --force)          FORCE=1; shift ;;
    *) echo "[migrate] unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Compose project defaults to the directory containing the compose file
# (here "single-host") unless COMPOSE_PROJECT_NAME is set. Volume names are
# "<project>_<volume>", so we need this to resolve them on the target.
PROJECT="${COMPOSE_PROJECT_NAME:-$(basename "$PWD")}"

log()  { echo "[migrate] $*"; }
die()  { echo "[migrate] ERROR: $*" >&2; exit 1; }
run()  { if [ "$DRY_RUN" -eq 1 ]; then echo "  + $*"; else eval "$*"; fi; }
confirm() {
  [ "$ASSUME_YES" -eq 1 ] && return 0
  printf "[migrate] %s [y/N] " "$1"; read -r ans
  case "$ans" in y|Y|yes|YES) return 0 ;; *) die "aborted by user" ;; esac
}

require_component() {
  case "$COMPONENT" in
    controller|executor|all) ;;
    "") die "--component is required (controller|executor|all)" ;;
    *) die "invalid --component: $COMPONENT" ;;
  esac
}

# Resolve the real docker volume name (compose prefixes with project name).
vol() {
  local suffix="$1" name
  name=$(docker volume ls --format '{{.Name}}' | grep -E "(^|_)${suffix}$" | head -n1 || true)
  [ -n "$name" ] || die "docker volume matching '*${suffix}' not found (is the stack created?)"
  echo "$name"
}

# tar a named volume's contents into OUT/<file> via a throwaway alpine container.
tar_volume() {
  local volname="$1" outfile="$2"
  run "docker run --rm -v ${volname}:/v -v \"$(cd "$OUT_DIR" && pwd)\":/out alpine \
        sh -c 'cd /v && tar czf /out/${outfile} .'"
}

# restore a tarball into a (freshly created) named volume.
untar_volume() {
  local volname="$1" infile="$2"
  run "docker volume create ${volname} >/dev/null"
  run "docker run --rm -v ${volname}:/v -v \"$(cd \"$(dirname "$infile")\" && pwd)\":/in alpine \
        sh -c 'cd /v && tar xzf /in/$(basename "$infile")'"
}

# ==========================================================================
# EXPORT
do_export() {
  require_component
  mkdir -p "$OUT_DIR"
  local stamp; stamp=$(date +%Y%m%d-%H%M%S)

  if [ "$COMPONENT" = "executor" ]; then
    [ -n "$CONTROLLER_URL" ] || die "--controller-url is required for executor export (the URL the new worker reaches the controller at, e.g. https://your-domain/)"
    [ -f .env ] || die ".env not found — run on the source deploy dir"
    local enroll; enroll=$(grep -E '^DLW_ENROLLMENT_TOKEN=' .env | cut -d= -f2-)
    local mc_user mc_pw
    mc_user=$(grep -E '^MINIO_ROOT_USER=' .env | cut -d= -f2-)
    mc_pw=$(grep -E '^MINIO_ROOT_PASSWORD=' .env | cut -d= -f2-)
    [ -n "$S3_URL" ] || S3_URL="$CONTROLLER_URL"  # best-effort default; usually a real MinIO/S3 endpoint
    local stage="$OUT_DIR/executor-bundle"
    rm -rf "$stage"; mkdir -p "$stage"
    # ship the build context an executor needs (src + Dockerfile + pyproject)
    log "staging executor build context"
    run "tar czf \"$stage/context.tgz\" -C ../.. pyproject.toml uv.lock alembic.ini README.md src Dockerfile.executor"
    # generate the standalone executor compose + env + runner
    cat > "$stage/.env" <<EOF
# Executor host config — generated by migrate.sh on ${stamp}.
DLW_ENROLLMENT_TOKEN=${enroll}
DLW_EXECUTOR_CONTROLLER_URL=${CONTROLLER_URL%/}
DLW_EXECUTOR_S3_ENDPOINT_URL=${S3_URL}
MINIO_ROOT_USER=${mc_user}
MINIO_ROOT_PASSWORD=${mc_pw}
DLW_LOG_LEVEL=INFO
# Give each worker host a UNIQUE id pair before first start:
DLW_EXECUTOR_HOST_ID=worker-host-2
DLW_EXECUTOR_ID=worker-host-2-worker-1
EOF
    cat > "$stage/docker-compose.executor.yml" <<'EOF'
# Standalone executor — runs on a worker host, talks to a remote controller.
# Prereqs: the controller URL below must be network-reachable from this host
# (via the controller's HTTPS reverse proxy, or an opened controller port),
# and the S3 endpoint must be reachable for uploads.
services:
  executor:
    build:
      context: ./context
      dockerfile: Dockerfile.executor
    container_name: dlw-executor
    restart: unless-stopped
    env_file: .env
    environment:
      DLW_EXECUTOR_BEARER_TOKEN: ${DLW_ENROLLMENT_TOKEN}
      DLW_EXECUTOR_DOWNLOAD_DIR: /downloads
      DLW_EXECUTOR_PARTS_DIR_PATH: /parts
      DLW_EXECUTOR_REGION: remote
      AWS_ACCESS_KEY_ID: ${MINIO_ROOT_USER}
      AWS_SECRET_ACCESS_KEY: ${MINIO_ROOT_PASSWORD}
    volumes:
      - dlw-exec-downloads:/downloads
      - dlw-exec-parts:/parts
      - ./logs:/var/log/dlw
    entrypoint: ["/bin/sh","-c"]
    command:
      - |
        mkdir -p /var/log/dlw
        exec dlw-executor --log-level ${DLW_LOG_LEVEL:-INFO} \
             2>&1 | tee -a /var/log/dlw/executor.log
volumes:
  dlw-exec-downloads:
  dlw-exec-parts:
EOF
    cat > "$stage/run-executor.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
[ -d context ] || { mkdir -p context && tar xzf context.tgz -C context; }
echo "[run-executor] EDIT .env first: set a UNIQUE DLW_EXECUTOR_ID / DLW_EXECUTOR_HOST_ID"
echo "[run-executor] controller URL = $(grep DLW_EXECUTOR_CONTROLLER_URL .env | cut -d= -f2-)"
mkdir -p logs
docker compose -f docker-compose.executor.yml up -d --build
echo "[run-executor] started. Verify on the controller:"
echo "  it should appear in GET /api/v1/executors and start polling (200)."
EOF
    chmod +x "$stage/run-executor.sh"
    local bundle="$OUT_DIR/modelpull-executor-${stamp}.tgz"
    run "tar czf \"$bundle\" -C \"$stage\" ."
    run "rm -rf \"$stage\""
    log "executor bundle → $bundle"
    log "next: scp it to the worker host, extract, edit .env (unique id), bash run-executor.sh"
    return 0
  fi

  # controller / all
  [ -f .env ] || die ".env not found — run in the source deploy dir"
  log "component=$COMPONENT — this snapshots PG + MinIO + CA + secrets."
  confirm "Stop controller+executors briefly for a consistent snapshot, then restart?"

  log "quiescing writers (controller + executors + frontend)"
  run "docker compose stop controller executor-1 executor-2 frontend || true"

  log "dumping PostgreSQL (custom format)"
  run "docker compose exec -T postgres pg_dump -U postgres -Fc dlw > \"$OUT_DIR/dlw.pgc\""

  log "archiving MinIO objects"
  tar_volume "$(vol dlw-miniodata)" "minio.tgz"
  log "archiving controller CA (mTLS signing material)"
  tar_volume "$(vol dlw-ca)" "ca.tgz"

  log "copying .env (secrets) + deploy files + build context"
  run "cp .env \"$OUT_DIR/env.snapshot\""
  run "tar czf \"$OUT_DIR/deploy-context.tgz\" -C ../.. \
        pyproject.toml uv.lock alembic.ini README.md MANUAL.md \
        src frontend config docs/operator \
        Dockerfile.controller Dockerfile.executor deploy/single-host"

  if [ "$INCLUDE_IMAGES" -eq 1 ]; then
    log "docker save controller + executor images (skips target rebuild)"
    run "docker save \$(docker compose config --images | sort -u | grep single-host) \
          | gzip > \"$OUT_DIR/images.tgz\" || true"
  fi

  log "restarting source stack (source is left fully intact)"
  run "docker compose up -d"

  local bundle="$OUT_DIR/modelpull-controller-${stamp}.tgz"
  run "tar czf \"$bundle\" -C \"$OUT_DIR\" \
        dlw.pgc minio.tgz ca.tgz env.snapshot deploy-context.tgz \
        $([ "$INCLUDE_IMAGES" -eq 1 ] && echo images.tgz)"
  log "controller bundle → $bundle"
  log "next: import it on the target with:  bash migrate.sh import --component $COMPONENT --in <bundle>"
}

# ==========================================================================
# IMPORT  (run on the TARGET host, inside a fresh /opt/modelpull/deploy/single-host)
do_import() {
  require_component
  [ -n "$IN_BUNDLE" ] || die "--in <bundle.tgz> is required"
  [ -f "$IN_BUNDLE" ] || die "bundle not found: $IN_BUNDLE"

  local work; work=$(mktemp -d)
  log "extracting bundle into $work"
  run "tar xzf \"$IN_BUNDLE\" -C \"$work\""

  if [ "$COMPONENT" = "executor" ]; then
    die "executor bundles are stood up with their own run-executor.sh, not 'import'. See the script header."
  fi

  # restore deploy files into the current dir's repo root. Exclude this
  # very script — it's already on disk (you're running it) and overwriting
  # a running bash file can corrupt the in-flight read.
  log "restoring deploy context (src/frontend/compose/Dockerfiles/.env)"
  run "tar xzf \"$work/deploy-context.tgz\" -C ../.. --exclude='deploy/single-host/migrate.sh'"
  run "cp \"$work/env.snapshot\" .env && chmod 600 .env"

  # refuse to clobber an existing populated stack
  if docker volume ls --format '{{.Name}}' | grep -qE "(^|_)dlw-pgdata$"; then
    [ "$FORCE" -eq 1 ] || die "target already has a dlw-pgdata volume. Re-run with --force to overwrite (DESTROYS target data)."
    log "--force: removing existing data volumes on target"
    run "docker compose down -v || true"
  fi

  log "loading images"
  if [ -f "$work/images.tgz" ]; then
    run "gunzip -c \"$work/images.tgz\" | docker load"
  fi

  log "bringing up postgres + minio (empty volumes)"
  run "docker compose up -d postgres minio"
  log "waiting for postgres healthy"
  run "until docker compose exec -T postgres pg_isready -U postgres -d dlw >/dev/null 2>&1; do sleep 2; done"

  log "restoring PostgreSQL dump"
  run "docker compose exec -T postgres pg_restore -U postgres -d dlw --clean --if-exists --no-owner < \"$work/dlw.pgc\" || true"

  log "restoring MinIO objects + CA into fresh volumes"
  # stop minio so we can repopulate its volume cleanly
  run "docker compose stop minio"
  untar_volume "${PROJECT}_dlw-miniodata" "$work/minio.tgz"
  untar_volume "${PROJECT}_dlw-ca"        "$work/ca.tgz"
  run "docker compose up -d minio"

  log "building + starting the full stack"
  run "docker compose up -d --build"

  log "waiting for controller /health/ready"
  run "until docker compose exec -T controller curl -fsS http://localhost:8001/health/ready >/dev/null 2>&1; do sleep 3; done"
  run "docker compose ps"
  run "rm -rf \"$work\""
  log "controller migrated. Point your reverse proxy / DNS at this host, then"
  log "repoint executors:  bash migrate.sh repoint-executors --controller-url <new-url>"
}

# ==========================================================================
# TRANSFER  (one-click: export here → scp → import on target)
do_transfer() {
  require_component
  [ -n "$TARGET" ] || die "--target user@host is required for transfer"

  if [ "$COMPONENT" = "executor" ]; then
    [ -n "$CONTROLLER_URL" ] || die "--controller-url required for executor transfer"
    do_export
    local bundle; bundle=$(ls -t "$OUT_DIR"/modelpull-executor-*.tgz | head -n1)
    log "scp $bundle → $TARGET:/tmp/"
    run "scp \"$bundle\" \"$TARGET:/tmp/$(basename "$bundle")\""
    run "ssh \"$TARGET\" 'mkdir -p /opt/modelpull-exec && tar xzf /tmp/$(basename "$bundle") -C /opt/modelpull-exec && cd /opt/modelpull-exec && bash run-executor.sh'"
    log "executor started on $TARGET. EDIT its /opt/modelpull-exec/.env for a unique id if running more than one."
    return 0
  fi

  do_export
  local bundle base; bundle=$(ls -t "$OUT_DIR"/modelpull-controller-*.tgz | head -n1)
  base=$(basename "$bundle")
  log "scp $bundle -> $TARGET:/tmp/$base"
  run "scp \"$bundle\" \"$TARGET:/tmp/$base\""
  # Bootstrap the target: pull ONLY deploy-context out of the bundle to lay
  # down deploy/single-host (incl. migrate.sh + src + compose), then run the
  # import there against the full bundle. import fails safe on a non-empty
  # target (refuses without --force), so we won't silently clobber data.
  log "laying down deploy files on target + running import over SSH"
  run "ssh \"$TARGET\" 'set -e; \
        mkdir -p $TARGET_DIR /tmp/dlw-mig; \
        tar xzf /tmp/$base -C /tmp/dlw-mig deploy-context.tgz; \
        tar xzf /tmp/dlw-mig/deploy-context.tgz -C $TARGET_DIR; \
        cd $TARGET_DIR/deploy/single-host; \
        bash migrate.sh import --component $COMPONENT --in /tmp/$base'"
  log "controller import finished on $TARGET."
  log "Now point DNS / reverse-proxy at $TARGET, then on each executor host run:"
  log "  bash migrate.sh repoint-executors --controller-url <new-controller-url>"
}

# ==========================================================================
# REPOINT-EXECUTORS — flip running executors at a new controller URL
do_repoint() {
  [ -n "$CONTROLLER_URL" ] || die "--controller-url <new-url> is required"
  [ -f docker-compose.yml ] || die "run in the deploy dir with the executors"
  log "repointing executors at ${CONTROLLER_URL%/}"
  # The compose pins DLW_EXECUTOR_CONTROLLER_URL=http://controller:8001 for the
  # co-located case. For a remote controller, override via .env + compose env.
  run "grep -q '^DLW_EXECUTOR_CONTROLLER_URL=' .env 2>/dev/null \
        && sed -i 's#^DLW_EXECUTOR_CONTROLLER_URL=.*#DLW_EXECUTOR_CONTROLLER_URL=${CONTROLLER_URL%/}#' .env \
        || echo 'DLW_EXECUTOR_CONTROLLER_URL=${CONTROLLER_URL%/}' >> .env"
  log "recreating executors"
  run "docker compose up -d executor-1 executor-2"
  log "done — confirm they poll 200 against the new controller in the logs."
}

case "$VERB" in
  export)            do_export ;;
  import)            do_import ;;
  transfer)          do_transfer ;;
  repoint-executors) do_repoint ;;
  ""|-h|--help|help)
    sed -n '2,60p' "$0" | sed 's/^# \{0,1\}//'
    ;;
  *) die "unknown verb: $VERB (export|import|transfer|repoint-executors)" ;;
esac
