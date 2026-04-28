#!/usr/bin/env bash
#
# RB-02 — Drain executor: gracefully remove from rotation
#
# What it does:
#   1. Tells controller to stop assigning new tasks to executor
#   2. Waits for in-flight subtasks to either complete or get reassigned
#   3. Confirms drain complete
#   4. (Optional) Deletes pod after drain
#
# Usage:
#   ./drain-executor.sh <executor-id> [--timeout 600] [--delete]
#
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <executor-id> [--timeout SEC] [--delete]"
  exit 2
fi

EXECUTOR_ID="$1"
shift
TIMEOUT=600
DELETE_POD=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --timeout) TIMEOUT="$2"; shift 2 ;;
    --delete) DELETE_POD=true; shift ;;
    *) echo "Unknown arg: $1"; exit 2 ;;
  esac
done

log() { printf '\033[36m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
fail() { printf '\033[31m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*" >&2; exit 1; }

DLW_TOKEN="${DLW_TOKEN:-}"
[[ -n "$DLW_TOKEN" ]] || fail "DLW_TOKEN env var required (admin token)"

API="${DLW_SERVER:-https://api.dlw.example.com}/api/v2"

# Step 1: tell controller to drain
log "Step 1/3: requesting drain of $EXECUTOR_ID"
HTTP_CODE=$(curl -s -o /tmp/drain_resp.json -w '%{http_code}' \
  -X POST "$API/admin/executors/$EXECUTOR_ID/drain" \
  -H "Authorization: Bearer $DLW_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"timeout_seconds\": $TIMEOUT}")

if [[ "$HTTP_CODE" != "202" ]]; then
  cat /tmp/drain_resp.json
  fail "Drain request rejected (HTTP $HTTP_CODE)"
fi

# Step 2: poll until drained
log "Step 2/3: waiting for in-flight subtasks (max ${TIMEOUT}s)"
deadline=$(($(date +%s) + TIMEOUT + 30))
while [[ $(date +%s) -lt $deadline ]]; do
  STATUS=$(curl -sf "$API/admin/executors/$EXECUTOR_ID" \
    -H "Authorization: Bearer $DLW_TOKEN" | jq -r '.status')
  RUNNING=$(curl -sf "$API/admin/executors/$EXECUTOR_ID" \
    -H "Authorization: Bearer $DLW_TOKEN" | jq -r '.running_subtasks // 0')
  log "  status=$STATUS running=$RUNNING"
  if [[ "$STATUS" == "drained" ]] && [[ "$RUNNING" == "0" ]]; then
    log "✓ Drained"
    break
  fi
  sleep 10
done

# Verify
STATUS=$(curl -sf "$API/admin/executors/$EXECUTOR_ID" \
  -H "Authorization: Bearer $DLW_TOKEN" | jq -r '.status')
if [[ "$STATUS" != "drained" ]]; then
  fail "Drain did not complete (status=$STATUS); some subtasks were force-released"
fi

# Step 3: (optional) delete pod
if [[ "$DELETE_POD" == "true" ]]; then
  log "Step 3/3: deleting pod"
  POD=$(kubectl -n dlw get pod -l app.kubernetes.io/component=executor \
    -o jsonpath="{.items[?(@.metadata.name=='$EXECUTOR_ID')].metadata.name}")
  if [[ -n "$POD" ]]; then
    kubectl -n dlw delete pod "$POD" --grace-period=10
    log "✓ Pod deleted; StatefulSet will recreate"
  fi
else
  log "Step 3/3: skipped (use --delete to remove pod)"
fi
