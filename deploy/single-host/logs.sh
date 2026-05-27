#!/usr/bin/env bash
# Quick log helper for the modelpull single-host deploy.
#
# Usage:
#   bash logs.sh tail [service]      — tail -F one service (default: controller)
#   bash logs.sh tail-all            — tail all logs interleaved
#   bash logs.sh errors [hours]      — grep WARNING/ERROR from last N hours (default 1)
#   bash logs.sh snapshot            — dump all logs to a timestamped tarball
#   bash logs.sh rotate              — force a fresh log file (renames current to .OLD)
#   bash logs.sh paths               — print the absolute path of every log file
#
# All commands operate on ./logs/ which docker-compose bind-mounts into
# every container as /var/log/dlw.

set -euo pipefail
cd "$(dirname "$0")"

LOG_DIR="$(realpath logs 2>/dev/null || echo logs)"

if [ ! -d "$LOG_DIR" ]; then
  echo "[logs.sh] $LOG_DIR doesn't exist. Has deploy.sh been run?" >&2
  exit 1
fi

cmd="${1:-tail}"

case "$cmd" in
  paths)
    echo "Log directory:  $LOG_DIR"
    for f in "$LOG_DIR"/*.log; do
      [ -e "$f" ] && printf "  %-30s  %s\n" "$(basename "$f")" "$(stat -c '%s bytes  modified %y' "$f" 2>/dev/null || stat -f '%z bytes  modified %Sm' "$f")"
    done
    echo
    echo "Also via docker (always available, includes containers that haven't written yet):"
    docker compose ps --format '  docker compose logs -f {{.Service}}'
    ;;

  tail)
    svc="${2:-controller}"
    file="$LOG_DIR/$svc.log"
    if [ -f "$file" ]; then
      echo "[logs.sh] tailing $file (Ctrl-C to stop)"
      tail -F "$file"
    else
      echo "[logs.sh] $file not found, falling back to docker compose logs -f $svc"
      docker compose logs -f "$svc"
    fi
    ;;

  tail-all)
    # GNU tail supports multi-file -F; on macOS it does too but interleaves
    # less cleanly. Use the lowest common denominator.
    files=("$LOG_DIR"/controller.log "$LOG_DIR"/executor-1.log "$LOG_DIR"/executor-2.log)
    existing=()
    for f in "${files[@]}"; do [ -e "$f" ] && existing+=("$f"); done
    if [ "${#existing[@]}" -eq 0 ]; then
      echo "[logs.sh] no log files yet; falling back to docker compose logs -f"
      docker compose logs -f
    else
      tail -F "${existing[@]}"
    fi
    ;;

  errors)
    hours="${2:-1}"
    cutoff=$(date -u -d "${hours} hours ago" '+%Y-%m-%d %H:%M' 2>/dev/null \
             || date -u -v -"${hours}"H '+%Y-%m-%d %H:%M')
    echo "[logs.sh] WARNING+ERROR+exception since UTC $cutoff (last ${hours}h)"
    echo
    # The Python logging default format starts with the ISO timestamp,
    # so an awk-based time filter works without parsing every line.
    for f in "$LOG_DIR"/*.log; do
      [ -e "$f" ] || continue
      svc=$(basename "$f" .log)
      hits=$(grep -E "(WARNING|ERROR|Exception|Traceback)" "$f" 2>/dev/null \
             | awk -v c="$cutoff" '$1" "$2 >= c' || true)
      if [ -n "$hits" ]; then
        echo "========== $svc =========="
        echo "$hits" | tail -n 80
        echo
      fi
    done
    ;;

  snapshot)
    out="logs-$(date +%Y%m%d-%H%M%S).tgz"
    docker compose ps > "$LOG_DIR/_ps-snapshot.txt" 2>&1 || true
    docker compose logs --no-color > "$LOG_DIR/_docker-logs-snapshot.txt" 2>&1 || true
    tar czf "$out" -C "$LOG_DIR" .
    rm -f "$LOG_DIR/_ps-snapshot.txt" "$LOG_DIR/_docker-logs-snapshot.txt"
    echo "[logs.sh] snapshot written: $(realpath "$out")"
    echo "[logs.sh] attach this when filing a bug; size: $(du -h "$out" | cut -f1)"
    ;;

  rotate)
    ts=$(date +%Y%m%d-%H%M%S)
    for f in "$LOG_DIR"/*.log; do
      [ -e "$f" ] || continue
      mv "$f" "${f}.${ts}"
      : > "${f}"
      echo "[logs.sh] rotated $(basename "$f")  → $(basename "$f").${ts}"
    done
    echo "[logs.sh] containers will append to fresh files on next write"
    echo "[logs.sh] for an IMMEDIATE flush use: docker compose restart controller executor-1 executor-2"
    ;;

  *)
    echo "Usage: bash logs.sh {tail [service] | tail-all | errors [hours] | snapshot | rotate | paths}" >&2
    exit 2
    ;;
esac
