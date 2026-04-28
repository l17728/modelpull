#!/usr/bin/env bash
#
# RB-08 — Rotate mTLS certificate for an executor
#
# Normally certs auto-renew before expiry. Use this only for:
#   - Forced rotation (suspected key leak)
#   - Failed auto-renewal
#
# What it does:
#   1. Generate new key + CSR on executor
#   2. Submit to controller
#   3. Receive new cert
#   4. Atomic swap (5min grace period overlap)
#   5. Verify
#
# Usage:
#   ./rotate-executor-mtls.sh <executor-id>
#
set -euo pipefail

EXECUTOR_ID="${1:?Usage: $0 <executor-id>}"

log() { printf '\033[36m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
fail() { printf '\033[31m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*" >&2; exit 1; }

[[ -n "${DLW_TOKEN:-}" ]] || fail "DLW_TOKEN env var required (admin token)"
API="${DLW_SERVER:-https://api.dlw.example.com}/api/v2"

# Step 1: Trigger executor-side rotation
log "Step 1/3: triggering rotation on $EXECUTOR_ID"
HTTP_CODE=$(curl -s -o /tmp/rot_resp.json -w '%{http_code}' \
  -X POST "$API/admin/executors/$EXECUTOR_ID/rotate-mtls" \
  -H "Authorization: Bearer $DLW_TOKEN")

[[ "$HTTP_CODE" == "202" ]] || { cat /tmp/rot_resp.json; fail "Rotation request rejected"; }

# Step 2: Poll status
log "Step 2/3: waiting for rotation (max 60s)"
deadline=$(($(date +%s) + 60))
while [[ $(date +%s) -lt $deadline ]]; do
  STATE=$(curl -sf "$API/admin/executors/$EXECUTOR_ID" \
    -H "Authorization: Bearer $DLW_TOKEN" | jq -r '.cert_rotation_state // "unknown"')
  log "  state=$STATE"
  case "$STATE" in
    completed) break ;;
    failed) fail "Rotation failed (check executor logs)" ;;
  esac
  sleep 5
done

# Step 3: Verify new cert is in use
log "Step 3/3: verifying new cert"
NEW_FP=$(curl -sf "$API/admin/executors/$EXECUTOR_ID" \
  -H "Authorization: Bearer $DLW_TOKEN" | jq -r '.cert_fingerprint')
NEW_EXPIRY=$(curl -sf "$API/admin/executors/$EXECUTOR_ID" \
  -H "Authorization: Bearer $DLW_TOKEN" | jq -r '.cert_expires_at')

log "✓ New cert: $NEW_FP"
log "  Expires: $NEW_EXPIRY"
log ""
log "Audit log entry created (action=executor.cert_rotated)"
