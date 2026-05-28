# Chaos Drill Runbook (v2.1 Sprint 15)

Four controlled-failure scenarios run alongside the steady-state Locust
load (`deploy/loadtest/locustfile.py`). Each drill has a one-line
acceptance condition; do not advance to the next drill until the previous
passes.

> **Scope:** staging only. Production deviates only after a green dry-run
> of every drill plus a written runbook of "if X breaks, what we do".

## Pre-flight

1. Confirm staging is green: `gh run list --branch main --limit 1`
2. Confirm Locust is running steady (>= 60 minutes warmup):
   `tail -F locust.log`
3. Confirm Grafana dashboards loaded:
   - `dlw-overview` (controller_role, throughput, p95)
   - `dlw-replication` (Sprint 6 dashboard)

## Drill 1 — Pull the PostgreSQL plug (5 min)

**Goal:** Verify controller fails read-write paths cleanly and recovers
when PG returns.

1. On a single PG instance, run:

   ```bash
   sudo systemctl stop postgresql
   ```

2. Within 2 minutes:
   - `/health/ready` returns 503 (DB unreachable)
   - All `POST /tasks` requests return 503; no 5xx body leaks an exception
     trace
   - Existing in-flight downloads pause (executors retry their next report)

3. Restart PG:

   ```bash
   sudo systemctl start postgresql
   ```

4. Within 5 minutes:
   - `/health/ready` returns 200
   - paused downloads resume
   - `/api/v1/admin/reverse-ws/sessions` lists every previously-connected
     executor (reverse-WSS reconnect-wins path from Sprint 10)

**Pass criterion:** Zero task rows in `running` status without an
`assigned_at` after PG comes back; no executor reports a "Connection
refused" loop lasting > 30 s.

## Drill 2 — Pull one S3 region (10 min)

**Goal:** Cross-region replication (Sprint 4-6) handles a target outage
gracefully.

1. Block egress to `region-B.s3.example.com` via VPC ACL.
2. Within 1 minute:
   - replication_worker logs `dlw_replication_jobs_total{status="failed"}`
     incrementing
   - Failed jobs hit `retry_count=3` and stay `failed` (no infinite retry)
3. Restore egress.
4. Manually re-create one failed job via REST:

   ```bash
   curl -X POST -H "Authorization: Bearer $TOKEN" \
        -d '{"source_object_id": <id>, "target_storage_id": <B-id>}' \
        $DLW_BASE/api/v1/replication
   ```

5. Within 5 minutes:
   - New job succeeds
   - `dlw_replication_bytes_total{status="succeeded"}` increments
   - Grafana `dlw-replication` dashboard's throughput panel shows recovery

**Pass criterion:** No spurious tombstones on the source object (Physical
GC must see refcount >= 1 throughout). No succeeded → failed flapping.

## Drill 3 — Kill the active controller (3 min)

**Goal:** Active/Standby leader election (W3c) flips within RTO.

1. Identify the active controller:

   ```bash
   kubectl get pods -l app=dlw-controller -o jsonpath='{.items[*].status.conditions[?(@.type=="Ready")].status}'
   # the active leader returns 200 on /health/active; standby returns 503
   for pod in $(kubectl get pods -l app=dlw-controller -o name); do
     echo "$pod $(kubectl exec $pod -- curl -s -o /dev/null -w '%{http_code}' localhost:8001/health/active)"
   done
   ```

2. Kill it:

   ```bash
   kubectl delete pod <active-pod> --grace-period=0
   ```

3. Within 30 s:
   - Standby promotes (Grafana leader panel flips)
   - `/health/active` on the surviving pod returns 200 (was 503 as standby)
   - In-flight downloads continue without progress loss (recovery routine
     re-claimed paused subtasks)

**Pass criterion:** Time from kill to "active" surfaced on the new pod
< 30 s; zero tasks transition to `failed` due to the kill.

## Drill 4 — Mass executor disconnect (5 min)

**Goal:** Reverse-WSS reconnect storm doesn't melt the controller.

1. Restart half of the executor StatefulSet:

   ```bash
   kubectl rollout restart statefulset/dlw-executor
   kubectl scale statefulset/dlw-executor --replicas=5  # was 10
   sleep 60
   kubectl scale statefulset/dlw-executor --replicas=10
   ```

2. Within 2 minutes:
   - `GET /api/v1/admin/reverse-ws/sessions` returns 10 entries again
   - No spike of 5xx on dashboard
   - `dlw_replication_jobs_total{status="failed"}` does NOT increment
     (replication should retry naturally on reconnect)

**Pass criterion:** All 10 executors are in `list_sessions()` within
2 minutes; controller CPU stays below 50% throughout.

## After the drills

1. Append the run to `docs/operator/sla-slo.md` § 3.
2. Generate a post-mortem doc from
   `docs/operator/post-mortem-template.md` for each drill that surfaced
   a follow-up.
3. Tag the build (`v2.1.0-rc.1` for the green run; bump on any fix).

## Rollback

If any drill fails: STOP. Capture full Grafana dashboards + controller
logs (`kubectl logs -l app=dlw-controller --since=1h --previous`) and
file an incident before iterating. Do not advance to the next drill on
a red result.
