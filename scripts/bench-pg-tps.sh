#!/usr/bin/env bash
# scripts/bench-pg-tps.sh — P-005 baseline: PostgreSQL transaction throughput.
#
# Per docs/v2.0/07-test-plan.md §perf P-005: target 5000 commits/s.
# Phase 1 produces a baseline on local PG; Phase 2 W2 entry criterion (§2.4)
# says: "Phase 1 实测 P-005 数据存在；如不达标先优化".
#
# Usage:
#   ./scripts/bench-pg-tps.sh
#   PGBENCH_SECONDS=120 PGBENCH_CLIENTS=20 ./scripts/bench-pg-tps.sh
#
# Prerequisites:
#   - postgresql-client (pgbench + createdb + dropdb + psql) in PATH
#   - DB on localhost:5433 with trust auth as user `postgres` (Phase 1 dev convention)
#
# Output:
#   - stdout: tee'd to docs/perf-baselines/p-005-pg-tps.txt (gitignored raw)
#   - operator hand-fills docs/perf-baselines/p-005-pg-tps.md with interpretation

set -euo pipefail

PG_HOST="${PGHOST:-localhost}"
PG_PORT="${PGPORT:-5433}"
PG_USER="${PGUSER:-postgres}"
BENCH_DB="${PGBENCH_DB:-pgbench_p005}"
BENCH_SCALE="${PGBENCH_SCALE:-10}"
BENCH_SECONDS="${PGBENCH_SECONDS:-60}"
BENCH_CLIENTS="${PGBENCH_CLIENTS:-10}"
BENCH_JOBS="${PGBENCH_JOBS:-4}"
OUT_DIR="${OUT_DIR:-docs/perf-baselines}"
OUT_TXT="$OUT_DIR/p-005-pg-tps.txt"

if ! command -v pgbench >/dev/null 2>&1; then
  echo "ERROR: pgbench not in PATH."
  echo "Install: brew install postgresql / apt install postgresql-contrib / similar"
  exit 2
fi

mkdir -p "$OUT_DIR"

echo "[1/4] Drop + recreate bench DB ($BENCH_DB) ..."
dropdb -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" --if-exists "$BENCH_DB"
createdb -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$BENCH_DB"

echo "[2/4] Initialize bench schema (scale=$BENCH_SCALE) ..."
pgbench -i -s "$BENCH_SCALE" -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$BENCH_DB"

echo "[3/4] Run TPS test ($BENCH_CLIENTS clients x $BENCH_JOBS jobs x ${BENCH_SECONDS}s) ..."
{
  echo "=== P-005 PG TPS baseline ==="
  echo "Date:        $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "Host:        $(uname -a)"
  echo "PG version:  $(psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$BENCH_DB" -t -c 'SHOW server_version' | xargs)"
  echo "DB scale:    $BENCH_SCALE"
  echo "Clients:     $BENCH_CLIENTS"
  echo "Jobs:        $BENCH_JOBS"
  echo "Duration:    ${BENCH_SECONDS}s"
  echo "Target:      5000 commits/s (07-test-plan §P-005)"
  echo "---"
  pgbench -c "$BENCH_CLIENTS" -j "$BENCH_JOBS" -T "$BENCH_SECONDS" \
    --progress=10 \
    -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$BENCH_DB"
} | tee "$OUT_TXT"

echo "[4/4] Result raw saved -> $OUT_TXT"
echo
echo "Next: review $OUT_TXT, then update docs/perf-baselines/p-005-pg-tps.md"
echo "      Result + Environment sections with the actual numbers."
