#!/usr/bin/env bash
#
# RB-12 — Enter / exit maintenance mode
#
# Maintenance mode behavior:
#   - Reject creation of new tasks (HTTP 503)
#   - Existing in-flight tasks continue
#   - Heartbeat continues (executors don't go offline)
#   - UI shows banner
#
# Usage:
#   ./maintenance.sh enter [--minutes 30] [--reason "schema migration"]
#   ./maintenance.sh exit
#   ./maintenance.sh status
#
set -euo pipefail

ACTION="${1:?Usage: $0 [enter|exit|status]}"
shift || true

API="${DLW_SERVER:-https://api.dlw.example.com}/api/v2"
[[ -n "${DLW_TOKEN:-}" ]] || { echo "DLW_TOKEN required"; exit 2; }

case "$ACTION" in
  enter)
    MINUTES=30
    REASON="planned maintenance"
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --minutes) MINUTES="$2"; shift 2 ;;
        --reason) REASON="$2"; shift 2 ;;
      esac
    done
    curl -sf -X POST "$API/admin/maintenance/enter" \
      -H "Authorization: Bearer $DLW_TOKEN" \
      -H "Content-Type: application/json" \
      -d "{\"freeze_minutes\": $MINUTES, \"reason\": \"$REASON\"}" | jq .
    echo "✓ Maintenance mode entered"
    ;;

  exit)
    curl -sf -X POST "$API/admin/maintenance/exit" \
      -H "Authorization: Bearer $DLW_TOKEN" | jq .
    echo "✓ Maintenance mode exited"
    ;;

  status)
    curl -sf "$API/admin/maintenance" \
      -H "Authorization: Bearer $DLW_TOKEN" | jq .
    ;;

  *) echo "Unknown action: $ACTION"; exit 2 ;;
esac
