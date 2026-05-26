# modelpull v2.1 Load Test

Locust scenarios for the Sprint 15 capacity-baseline run. Targets the
acceptance criteria from `docs/v2.1-sprint-plan.md` §S15:

  - 7 days continuous run, ≥ 99.5% controller availability
  - p95 API latency < 300 ms under steady-state load
  - 1000 tasks / 100 concurrent users / mixed read+write workload

## Quick start

```bash
pip install 'locust>=2.30'
export DLW_BASE_URL=https://staging.modelpull.internal
export DLW_JWT=$(./deploy/runbooks/scripts/maintenance.sh --issue-jwt tenant_admin)
locust -f deploy/loadtest/locustfile.py \
       --headless -u 100 -r 10 -t 7d \
       --csv staging-baseline
```

Flags:

| Flag | Meaning |
|------|---------|
| `-u 100` | 100 concurrent virtual users |
| `-r 10`  | spawn 10 users per second (gentle ramp) |
| `-t 7d`  | 7-day continuous run |
| `--csv`  | CSV reports (per-request + per-second + per-failure) |

Pre-flight: at least 10 executors registered, ≥ 3 storage backends
configured with at-rest envelope encryption (`DLW_CONFIG_KEY` set).

## Scenarios

The locustfile defines four user types with weighted traffic:

| User type | Weight | Endpoints |
|-----------|--------|-----------|
| `BrowsingUser`  | 60% | `/quota/current`, `/tasks?status=running`, `/audit` |
| `SubmittingUser` | 20% | `/tasks` (create) — one task / 30s per user |
| `AdminUser`     | 15% | `/admin/reverse-ws/sessions`, executor list |
| `AIUser`        |  5% | `/ai/chat` (read-only tool flows only) |

Token usage: every request uses a per-user JWT issued from the
`tenant_admin` role; admin requests use a `system_admin` JWT.

## Acceptance gates

The run is GREEN only if all four hold across the 7-day window:

1. **Availability**: 0 minutes of all-2xx-on-`/healthz` downtime exceeding
   3 minutes (matches the controller's leader-promotion SLA).
2. **Latency**: per-endpoint p95 < 300 ms on every 1-hour bucket except
   leader-failover events (allow one 60s spike per drill).
3. **Throughput**: ≥ 5 task-create QPS sustained without 5xx beyond the
   quota-exceeded rejections (those are 4xx and don't count).
4. **No data loss**: after the run, every queued task is in a terminal
   state (succeeded / failed / cancelled). Zero rows stuck in `running`
   for >2h.

## Mapping back to docs

After the run, fill in `docs/operator/sla-slo.md` § 3 "容量基线" table —
replace every ❓ entry with measured value + the locust run ID.

## Chaos overlay

The plain locustfile keeps the system steady. For chaos drills run it
in parallel with the steps in `deploy/runbooks/chaos-drill.md` — that
runbook lists the 4 controlled failures (PG drop, region drop, leader
kill, executor mass-disconnect) and how to score each one.
