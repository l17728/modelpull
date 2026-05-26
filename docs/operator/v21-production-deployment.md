# v2.1 Production Deployment Checklist

End-to-end checklist for promoting a v2.1 staging deployment to
production. Follow in order; each step is independent and has its own
rollback. Do not run multiple steps in parallel during the first
production cutover.

## Pre-flight (before the deploy window)

- [ ] All 14 sprints of `docs/v2.1-sprint-plan.md` show ✅
- [ ] CI is green on `main` for the version being deployed
- [ ] Every `deploy/runbooks/chaos-drill.md` drill is green on staging
- [ ] Locust run finished with 0 failed acceptance gates
- [ ] `docs/operator/sla-slo.md` § 3 capacity baseline filled in
- [ ] Helm values diff vs. previous prod review-approved
- [ ] `DLW_CONFIG_KEY` issued + stored in production secrets manager
- [ ] On-call schedule covers the deploy window + 48h after

## Deploy step 1 — Schema migration

1. Backup PG: `deploy/runbooks/scripts/verify-backup.sh production`
2. Run alembic upgrade head on the production DB (no app traffic yet):

   ```bash
   DLW_DB_NAME=dlw_prod uv run alembic upgrade head
   ```

3. Verify new tables exist:

   ```sql
   SELECT tablename FROM pg_tables WHERE schemaname='public'
     AND tablename IN ('replication_jobs',
                        'chunk_throughput_sample',
                        'storage_physical_keys');
   ```

   All three should be present.

**Rollback**: `alembic downgrade <previous_revision>` then restore the
PG backup if needed.

## Deploy step 2 — Controller rollout (active/standby)

1. Helm upgrade with new image tag — k8s rolling update handles the
   standby first:

   ```bash
   helm upgrade dlw deploy/helm/ -f values.prod.yaml --atomic
   ```

2. Watch the rollout:

   ```bash
   kubectl rollout status deployment/dlw-controller -w
   ```

3. Verify both pods are alive AND only one is `controller_role=active`:

   ```bash
   for pod in $(kubectl get pods -l app=dlw-controller -o name); do
     echo "$pod"
     kubectl exec $pod -- curl -s localhost:8001/healthz | jq .controller_role
   done
   ```

**Rollback**: `helm rollback dlw` — the helm chart pins the previous
version and rolls back atomically.

## Deploy step 3 — Executors

1. Rolling restart the executor StatefulSet:

   ```bash
   kubectl rollout restart statefulset/dlw-executor
   ```

2. Watch the reverse-WSS sessions repopulate:

   ```bash
   curl -H "Authorization: Bearer $ADMIN_TOKEN" \
        $DLW_BASE/api/v1/admin/reverse-ws/sessions | jq '.items | length'
   ```

   Should match the executor replica count within 5 minutes.

**Rollback**: `kubectl rollout undo statefulset/dlw-executor`.

## Deploy step 4 — Feature flags (gradual enable)

All v2.1 features ship OFF by default. Enable progressively over the
first 7 days:

| Day | Flag | Reason for gating |
|-----|------|-------------------|
| 1   | `DLW_SLA_TIER_ENABLED=true` | Tier defaults to `standard`; harmless on day 1 |
| 2   | `DLW_PHYSICAL_GC_ENABLED=true` | Run tombstone scan only; LRU eviction next sprint |
| 3   | `DLW_THROUGHPUT_SAMPLER_ENABLED=true` | Already on by default; verify samples land |
| 4   | `DLW_REPLICATION_WORKER_ENABLED=true` | Verify with a single test replication job first |
| 5   | `DLW_ADAPTIVE_OPTIMIZER_ENABLED=true` | SHADOW mode only on day 5 |
| 7   | `DLW_ADAPTIVE_OPTIMIZER_APPLY=true` | Only after 48h of shadow-log review |

`DLW_CONFIG_KEY` (Sprint 12 envelope encryption) is set on day 0 so new
storage_backends rows wrap automatically; legacy rows stay plaintext and
will be re-encrypted in a follow-up batch job (TBD).

## Smoke test (post each step)

```bash
./deploy/runbooks/scripts/maintenance.sh smoke production
```

Expected: exit 0, all 12 smoke checks pass.

## Verification (T+24h)

- [ ] All 4 chaos drills repeated successfully on prod with synthetic
      traffic only (use a separate tenant ID)
- [ ] `/metrics` exposes all 5 v2.1 metric series (replication ×3,
      optimizer solve duration, replan moves)
- [ ] Grafana dashboards populated; alert rules firing-clean
- [ ] One post-mortem template exercise — pick a drill from staging and
      write it up in `docs/operator/post-mortem-template.md` shape, even
      though no real incident occurred. This proves the process works
      before you need it.

## Communication

- T-7d: announce the deployment window in `#modelpull-announce`
- T-1d: confirm with on-call + tenants of any v2.1-feature-specific
  changes (e.g. SLA tier changes)
- T+0: post the rollout SHA + helm chart version
- T+24h: post the verification status + any rollback decisions
- T+7d: post the load-test summary + finalized SLO numbers

## Related docs

- `docs/v2.1-sprint-plan.md` — full feature scope
- `docs/v2.1-roadmap.md` — status matrix
- `deploy/runbooks/chaos-drill.md` — failure rehearsals
- `docs/operator/sla-slo.md` — SLI/SLO definitions + capacity baseline
- `docs/operator/post-mortem-template.md` — incident write-ups
