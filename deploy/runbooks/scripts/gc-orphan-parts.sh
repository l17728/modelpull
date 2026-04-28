#!/usr/bin/env bash
#
# RB-06 — GC orphan parts: clean .parts/ directories with no active downloads
#
# Run on each executor; or as a CronJob in K8s.
#
# Removes .parts/ subdirectories where:
#   - mtime > 24h ago AND
#   - corresponding subtask is not in {assigned, downloading, uploading, paused_*}
#
# Safe to run multiple times. Read-only mode: --dry-run
#
# Usage:
#   ./gc-orphan-parts.sh [--parts-dir /var/lib/dlw/parts] [--ttl-hours 24] [--dry-run]
#
set -euo pipefail

PARTS_DIR="${PARTS_DIR:-/var/lib/dlw/parts}"
TTL_HOURS=24
DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --parts-dir) PARTS_DIR="$2"; shift 2 ;;
    --ttl-hours) TTL_HOURS="$2"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    *) echo "Unknown: $1"; exit 2 ;;
  esac
done

log() { printf '\033[36m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }

[[ -d "$PARTS_DIR" ]] || { log "$PARTS_DIR does not exist; nothing to do"; exit 0; }

# Get list of subtask IDs that are still active
ACTIVE_IDS_FILE=$(mktemp)
trap "rm -f $ACTIVE_IDS_FILE" EXIT

API="${DLW_CONTROLLER_ENDPOINT:-https://controller.dlw.svc.cluster.local:8000}/api/v2"
EXECUTOR_ID="${DLW_EXECUTOR_ID:-$(hostname)}"

# Internal API: list all active subtasks for this executor
curl -sf --cacert /etc/dlw/ca/tls.crt \
  --cert /etc/dlw/client.crt --key /etc/dlw/client.key \
  "$API/internal/executors/$EXECUTOR_ID/active-subtasks" | \
  jq -r '.subtask_ids[]' > "$ACTIVE_IDS_FILE" || {
    log "WARN: Could not contact controller; falling back to TTL-only GC"
    : > "$ACTIVE_IDS_FILE"
  }

ACTIVE_COUNT=$(wc -l < "$ACTIVE_IDS_FILE")
log "Active subtasks on this executor: $ACTIVE_COUNT"

# Iterate parts subdirectories
TOTAL_FREED=0
TOTAL_REMOVED=0

while IFS= read -r dir; do
  subtask_id=$(basename "$dir")

  # Check if active
  if grep -qx "$subtask_id" "$ACTIVE_IDS_FILE"; then
    continue
  fi

  # Check TTL
  mtime_epoch=$(stat -c %Y "$dir" 2>/dev/null || echo 0)
  age_hours=$(( ($(date +%s) - mtime_epoch) / 3600 ))
  if (( age_hours < TTL_HOURS )); then
    continue
  fi

  # Compute size
  bytes=$(du -sb "$dir" 2>/dev/null | awk '{print $1}')
  human=$(numfmt --to=iec --suffix=B "$bytes")

  log "Found orphan: $subtask_id (age ${age_hours}h, size $human)"

  if [[ "$DRY_RUN" == "true" ]]; then
    log "  [DRY-RUN] would rm -rf $dir"
  else
    rm -rf "$dir"
    log "  ✓ Removed"
  fi

  TOTAL_FREED=$((TOTAL_FREED + bytes))
  TOTAL_REMOVED=$((TOTAL_REMOVED + 1))
done < <(find "$PARTS_DIR" -maxdepth 1 -mindepth 1 -type d)

log "Summary: removed $TOTAL_REMOVED orphan dirs, freed $(numfmt --to=iec --suffix=B "$TOTAL_FREED")"
