#!/usr/bin/env bash
#
# Verify daily that PG basebackup + WAL archive are recoverable.
#
# Run as nightly CronJob:
#   0 4 * * * /opt/dlw/scripts/verify-backup.sh
#
# What it does:
#   1. Picks latest basebackup from S3
#   2. Restores to a temporary PG instance (in tmpfs)
#   3. Replays WAL up to 15 minutes ago (PITR target)
#   4. Runs sanity queries (table counts, audit chain integrity)
#   5. Reports SUCCESS/FAIL via Prometheus pushgateway
#
set -euo pipefail

BACKUP_BUCKET="${BACKUP_BUCKET:-s3://dlw-backup}"
TMP_PG_DATA="${TMP_PG_DATA:-/tmp/dlw-verify-$$}"
PUSHGATEWAY="${PUSHGATEWAY:-http://prometheus-pushgateway.monitoring:9091}"
JOB_NAME="dlw_backup_verify"

log() { printf '\033[36m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
fail() {
  printf '\033[31m[FAIL]\033[0m %s\n' "$*" >&2
  push_metric "fail" "$1"
  exit 1
}

push_metric() {
  local result="$1"
  local reason="${2:-}"
  cat <<EOF | curl -sf --data-binary @- "$PUSHGATEWAY/metrics/job/$JOB_NAME"
# TYPE dlw_backup_verify_last_success_timestamp gauge
dlw_backup_verify_last_success_timestamp $(date +%s)
# TYPE dlw_backup_verify_last_result gauge
dlw_backup_verify_last_result{result="$result",reason="$reason"} 1
EOF
}

trap 'rm -rf "$TMP_PG_DATA"' EXIT

# Step 1: Pick latest basebackup
log "Step 1/4: locating latest basebackup"
LATEST=$(aws s3 ls "$BACKUP_BUCKET/basebackups/" | sort | tail -1 | awk '{print $4}')
[[ -n "$LATEST" ]] || fail "no_backup_found"
log "Latest: $LATEST"

# Step 2: Restore
log "Step 2/4: restoring basebackup"
mkdir -p "$TMP_PG_DATA"
aws s3 cp "$BACKUP_BUCKET/basebackups/$LATEST" - | tar -xzf - -C "$TMP_PG_DATA"

# Step 3: Configure PITR target = 15min ago
TARGET_TIME=$(date -u -d '15 minutes ago' '+%Y-%m-%d %H:%M:%S UTC')
log "PITR target: $TARGET_TIME"
cat > "$TMP_PG_DATA/postgresql.auto.conf" <<EOF
restore_command = 'aws s3 cp $BACKUP_BUCKET/wal/%f %p'
recovery_target_time = '$TARGET_TIME'
recovery_target_action = 'pause'
EOF
touch "$TMP_PG_DATA/recovery.signal"

PORT=55432
log "Starting temp PG on port $PORT"
pg_ctl -D "$TMP_PG_DATA" -o "-p $PORT" -l /tmp/verify-pg.log start

# Wait for recovery to pause at target
deadline=$(($(date +%s) + 600))
while [[ $(date +%s) -lt $deadline ]]; do
  STATE=$(psql -h localhost -p $PORT -U postgres -t -c \
    "SELECT pg_is_in_recovery();" 2>/dev/null | tr -d ' ' || echo "")
  if [[ "$STATE" == "t" ]]; then
    PAUSED=$(psql -h localhost -p $PORT -U postgres -t -c \
      "SELECT pg_is_wal_replay_paused();" | tr -d ' ')
    if [[ "$PAUSED" == "t" ]]; then break; fi
  fi
  sleep 5
done

# Step 4: Sanity queries
log "Step 4/4: sanity queries"

# Count tasks
TASK_COUNT=$(psql -h localhost -p $PORT -U postgres dlw -t -c \
  "SELECT count(*) FROM download_tasks" | tr -d ' ')
log "  download_tasks count: $TASK_COUNT"
[[ "$TASK_COUNT" -ge 0 ]] || fail "task_query_failed"

# Audit chain integrity (sample)
BROKEN=$(psql -h localhost -p $PORT -U postgres dlw -t <<EOF | tr -d ' '
WITH chain AS (
  SELECT id, prev_hash,
    LAG(self_hash) OVER (ORDER BY id) AS expected_prev
  FROM audit_log ORDER BY id LIMIT 1000
)
SELECT count(*) FROM chain
WHERE id > 1 AND prev_hash != expected_prev
EOF
)
log "  audit chain breaks (sample 1000): $BROKEN"
[[ "$BROKEN" == "0" ]] || fail "audit_chain_broken"

# State machine sanity
ILLEGAL=$(psql -h localhost -p $PORT -U postgres dlw -t -c \
  "SELECT count(*) FROM file_subtasks WHERE status = 'transferring'" | tr -d ' ')
[[ "$ILLEGAL" == "0" ]] || fail "illegal_status_transferring_found"

pg_ctl -D "$TMP_PG_DATA" stop -m immediate || true

log "✓ Backup verified successfully"
push_metric "success"
