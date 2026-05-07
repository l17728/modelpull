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

# Wait for PG to actually accept connections (CODE-07 修复 v2.0.12)
log "Waiting for PG to be ready (max 60s)..."
if ! pg_isready -h localhost -p "$PORT" -t 60 -U postgres >/dev/null; then
  fail "pg_not_ready_after_recovery"
fi

# Helper: run single-line SQL with -tAc (tuples-only, unaligned, command); single quoted result
# CODE-07 修复 v2.0.12: 替代有问题的 heredoc + tr 残破方案
psql_q() {
  local sql="$1"
  local result
  if ! result=$(psql -h localhost -p "$PORT" -U postgres dlw -tAc "$sql" 2>&1); then
    echo "PSQL_ERROR: $result" >&2
    return 1
  fi
  if [[ -z "$result" ]]; then
    echo "PSQL_EMPTY" >&2
    return 1
  fi
  printf '%s' "$result"
}

# Step 4: Sanity queries
log "Step 4/4: sanity queries"

# Count tasks (CODE-07 修复: 使用 psql_q 处理空字符串)
TASK_COUNT=$(psql_q "SELECT count(*) FROM download_tasks") || fail "task_query_failed"
log "  download_tasks count: $TASK_COUNT"
if ! [[ "$TASK_COUNT" =~ ^[0-9]+$ ]]; then
  fail "task_count_invalid:$TASK_COUNT"
fi

# Audit chain integrity (sample first 1000 rows)
# CODE-07 修复: 用单行 SQL + psql -tAc，避免 heredoc + tr 的 silent fail 路径
BROKEN=$(psql_q "WITH chain AS (SELECT id, prev_hash, LAG(self_hash) OVER (ORDER BY id) AS expected_prev FROM audit_log ORDER BY id LIMIT 1000) SELECT count(*) FROM chain WHERE id > 1 AND prev_hash != expected_prev") \
  || fail "audit_query_failed"
log "  audit chain breaks (sample 1000): $BROKEN"
if ! [[ "$BROKEN" =~ ^[0-9]+$ ]]; then
  fail "audit_count_invalid:$BROKEN"
fi
if [[ "$BROKEN" -ne 0 ]]; then
  fail "audit_chain_broken:$BROKEN"
fi

# State machine sanity (CODE-07 同样使用 psql_q)
ILLEGAL=$(psql_q "SELECT count(*) FROM file_subtasks WHERE status = 'transferring'") \
  || fail "state_machine_query_failed"
if ! [[ "$ILLEGAL" =~ ^[0-9]+$ ]]; then
  fail "illegal_count_invalid:$ILLEGAL"
fi
if [[ "$ILLEGAL" -ne 0 ]]; then
  fail "illegal_status_transferring_found:$ILLEGAL"
fi

# Additional check: no NULL prev_hash (id > 1)
NULL_HASH=$(psql_q "SELECT count(*) FROM audit_log WHERE id > 1 AND prev_hash IS NULL") \
  || fail "null_hash_query_failed"
if [[ "$NULL_HASH" -ne 0 ]]; then
  fail "audit_null_prev_hash:$NULL_HASH"
fi

pg_ctl -D "$TMP_PG_DATA" stop -m immediate || true

log "✓ Backup verified successfully"
push_metric "success"
