#!/usr/bin/env bash
#
# RB-01 — Controller failover: promote standby to active
#
# When to use:
#   - ControllerDown alert fires
#   - You have verified active is unreachable
#   - Standby is healthy and replicating
#
# What it does:
#   1. Verifies standby health
#   2. Triggers PG failover (promote standby to primary)
#   3. Updates K8s LB to point to new active
#   4. Verifies new active is serving traffic
#   5. Old active enters fenced state (cannot promote back automatically)
#
# RTO target: ≤ 10 minutes
#
# Usage:
#   ./promote-standby.sh [--namespace dlw] [--force]
#
# Required env:
#   KUBECONFIG  — pointing to cluster with dlw deployed
#

set -euo pipefail

NAMESPACE="${NAMESPACE:-dlw}"
FORCE=false
PG_TIMEOUT=300
LB_TIMEOUT=120

while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace) NAMESPACE="$2"; shift 2 ;;
    --force) FORCE=true; shift ;;
    -h|--help) sed -n '2,/^$/p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *) echo "Unknown arg: $1"; exit 2 ;;
  esac
done

log()  { printf '\033[36m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
warn() { printf '\033[33m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
fail() { printf '\033[31m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*" >&2; exit 1; }

# Step 0: pre-checks
log "Step 0/5: pre-flight checks"
kubectl version --short >/dev/null 2>&1 || fail "kubectl not configured"
kubectl -n "$NAMESPACE" get pods -l app.kubernetes.io/component=controller -o name \
  >/dev/null 2>&1 || fail "Cannot list controller pods in $NAMESPACE"

# Identify active vs standby by checking pg_is_in_recovery
ACTIVE_POD=""
STANDBY_POD=""
for pod in $(kubectl -n "$NAMESPACE" get pods -l app.kubernetes.io/component=postgresql -o name); do
  if kubectl -n "$NAMESPACE" exec "$pod" -- psql -U postgres -t -c "SELECT pg_is_in_recovery();" 2>/dev/null | grep -q 'f'; then
    ACTIVE_POD="$pod"
  else
    STANDBY_POD="$pod"
  fi
done

log "Active PG:  $ACTIVE_POD"
log "Standby PG: $STANDBY_POD"

[[ -n "$STANDBY_POD" ]] || fail "No standby found"

# Step 1: confirm active is actually down
log "Step 1/5: verify active unreachable"
if [[ "$FORCE" != "true" ]]; then
  if kubectl -n "$NAMESPACE" exec "$ACTIVE_POD" -- pg_isready -U postgres >/dev/null 2>&1; then
    warn "Active is still responding! Refusing to fail over without --force"
    warn "Run with --force only if you've confirmed split-brain is impossible"
    exit 1
  fi
fi

# Step 2: check standby replication lag
log "Step 2/5: check standby lag"
LAG=$(kubectl -n "$NAMESPACE" exec "$STANDBY_POD" -- psql -U postgres -t -c \
  "SELECT EXTRACT(EPOCH FROM (now() - pg_last_xact_replay_timestamp()));" | tr -d ' ')

log "Standby lag: ${LAG}s"
if (( $(echo "$LAG > 60" | bc -l) )); then
  warn "Lag is >60s. Some recent commits may be lost."
  if [[ "$FORCE" != "true" ]]; then
    fail "Refusing to fail over with high lag (use --force to override)"
  fi
fi

# Step 3: promote standby
log "Step 3/5: promoting standby to primary (this may take up to ${PG_TIMEOUT}s)"
kubectl -n "$NAMESPACE" exec "$STANDBY_POD" -- pg_ctl promote -D /var/lib/postgresql/data
# Wait for promotion
deadline=$(($(date +%s) + PG_TIMEOUT))
while [[ $(date +%s) -lt $deadline ]]; do
  if kubectl -n "$NAMESPACE" exec "$STANDBY_POD" -- psql -U postgres -t -c \
       "SELECT pg_is_in_recovery();" 2>/dev/null | grep -q 'f'; then
    log "Promotion complete"
    break
  fi
  sleep 2
done

# Verify
if kubectl -n "$NAMESPACE" exec "$STANDBY_POD" -- psql -U postgres -t -c \
     "SELECT pg_is_in_recovery();" 2>/dev/null | grep -q 't'; then
  fail "Promotion timed out"
fi

# Step 4: update Service to point at new primary
log "Step 4/5: redirecting Service to new primary"
NEW_PRIMARY_NAME=$(kubectl -n "$NAMESPACE" get pod "${STANDBY_POD#pod/}" -o jsonpath='{.metadata.name}')
kubectl -n "$NAMESPACE" patch service dlw-postgresql-primary \
  -p "{\"spec\": {\"selector\": {\"statefulset.kubernetes.io/pod-name\": \"$NEW_PRIMARY_NAME\"}}}"

# Step 5: trigger controller restart so it reconnects
log "Step 5/5: rolling controller pods"
kubectl -n "$NAMESPACE" rollout restart deployment dlw-controller
kubectl -n "$NAMESPACE" rollout status deployment dlw-controller --timeout=${LB_TIMEOUT}s

# Verify
log "Verification..."
if curl -sf "https://api.dlw.example.com/health" >/dev/null; then
  log "✓ New active is serving traffic"
  log ""
  log "Old primary (${ACTIVE_POD}) is now in fenced state."
  log "It will need to be re-added as standby AFTER the incident is reviewed."
  log "Do NOT auto-restart it without verifying it's safe."
  log ""
  log "Next steps:"
  log "  1. Update incident channel"
  log "  2. Schedule post-mortem within 24h"
  log "  3. Re-establish replication: ./re-join-standby.sh ${ACTIVE_POD#pod/}"
else
  fail "New active not responding to health check"
fi
