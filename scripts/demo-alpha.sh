#!/usr/bin/env bash
# scripts/demo-alpha.sh — Phase 1 alpha demo, single command from cold start.
#
# What it does:
#   1. Boots the full stack via docker-compose (PG + controller + executor +
#      MinIO + bucket-init).
#   2. Waits for /health/ready.
#   3. Seeds a download task pointing to a small public HuggingFace model
#      (~90MB, multi-file).
#   4. Polls task status until it hits a terminal state OR timeout (180s).
#   5. Prints a summary: per-subtask status + S3 keys + MinIO console URL.
#
# Prerequisites:
#   - docker + docker compose installed
#   - ports 5433 / 8000 / 9000 / 9001 free
#   - DLW_BEARER_TOKEN set (defaults to dev-token-change-me to match docker-compose)
#
# Stop the stack with:  docker compose -f docker-compose.dev.yml down -v
#
# UI demo: in another terminal, `cd frontend && pnpm install && pnpm dev`,
# then http://localhost:5173 (paste $DLW_BEARER_TOKEN to log in).

set -euo pipefail

REPO_ID="${DEMO_REPO_ID:-sentence-transformers/all-MiniLM-L6-v2}"
REVISION="${DEMO_REVISION:-main}"
TOKEN="${DLW_BEARER_TOKEN:-dev-token-change-me}"
COMPOSE_FILE="docker-compose.dev.yml"
TIMEOUT_SECONDS="${DEMO_TIMEOUT:-180}"

echo "================================================================"
echo "  modelpull Phase 1 alpha demo"
echo "  repo = $REPO_ID @ $REVISION"
echo "================================================================"

echo
echo "[1/5] Boot stack (PG + controller + executor + MinIO + bucket-init)..."
docker compose -f "$COMPOSE_FILE" up -d --build

echo
echo "[2/5] Wait for controller /health/ready..."
deadline=$(( $(date +%s) + 60 ))
until curl -fs http://localhost:8000/health/ready >/dev/null 2>&1; do
  if [ "$(date +%s)" -gt "$deadline" ]; then
    echo "FAIL: controller did not become ready within 60s"
    docker compose -f "$COMPOSE_FILE" logs controller | tail -30
    exit 1
  fi
  sleep 1
done
echo "      controller ready"

echo
echo "[3/5] POST /api/v1/tasks ..."
TASK_ID=$(curl -fs -X POST http://localhost:8000/api/v1/tasks \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"repo_id\":\"$REPO_ID\",\"revision\":\"$REVISION\",\"storage_id\":1}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "      task_id = $TASK_ID"

echo
echo "[4/5] Poll until terminal state (timeout=${TIMEOUT_SECONDS}s)..."
deadline=$(( $(date +%s) + TIMEOUT_SECONDS ))
prev_status=""
while true; do
  RESP=$(curl -fs "http://localhost:8000/api/v1/tasks/$TASK_ID" \
    -H "Authorization: Bearer $TOKEN")
  STATUS=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
  if [ "$STATUS" != "$prev_status" ]; then
    echo "      [$(date +%H:%M:%S)] status: $STATUS"
    prev_status="$STATUS"
  fi
  case "$STATUS" in
    succeeded|failed|cancelled) break ;;
  esac
  if [ "$(date +%s)" -gt "$deadline" ]; then
    echo "FAIL: task did not reach terminal state within ${TIMEOUT_SECONDS}s"
    docker compose -f "$COMPOSE_FILE" logs executor | tail -30
    exit 1
  fi
  sleep 2
done

echo
echo "[5/5] Summary:"
curl -fs "http://localhost:8000/api/v1/tasks/$TASK_ID" \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -c "
import sys, json
t = json.load(sys.stdin)
print(f\"      task          {t['id'][:8]}…\")
print(f\"      repo          {t['repo_id']}@{t['revision'][:12]}…\")
print(f\"      status        {t['status']}\")
print(f\"      created_at    {t['created_at']}\")
print(f\"      completed_at  {t.get('completed_at') or '—'}\")
print(f\"      subtasks      {len(t['subtasks'])}\")
ok = sum(1 for s in t['subtasks'] if s['status'] == 'succeeded')
print(f\"      ✓ succeeded   {ok}/{len(t['subtasks'])}\")
for s in t['subtasks']:
    sk = s.get('s3_key', '—')
    print(f\"        {s['status']:<10} {s['filename']:<30} → {sk}\")
"

echo
echo "================================================================"
echo "  Demo complete."
echo "  MinIO console:    http://localhost:9001  (minioadmin / minioadmin)"
echo "  Task detail JSON: curl http://localhost:8000/api/v1/tasks/$TASK_ID -H 'Authorization: Bearer $TOKEN' | jq"
echo
echo "  Stop the stack with:"
echo "    docker compose -f $COMPOSE_FILE down -v"
echo "================================================================"
