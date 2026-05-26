# Post-Mortem Template — modelpull v2.1

Fill in within 48 hours of incident resolution. Use the blameless tone
prescribed by the SRE handbook: focus on system + process, never people.

## Incident header

| Field | Value |
|-------|-------|
| Incident ID | `YYYY-MM-DD-short-slug` |
| Severity | SEV1 / SEV2 / SEV3 (see severity matrix below) |
| Started | `<UTC ISO timestamp>` |
| Detected | `<UTC ISO timestamp>` |
| Mitigated | `<UTC ISO timestamp>` |
| Resolved | `<UTC ISO timestamp>` |
| Time to detect | `<minutes>` |
| Time to mitigate | `<minutes>` |
| User-visible impact | one sentence |
| Authors | `@name1, @name2` |

### Severity matrix

| Severity | Definition |
|----------|------------|
| SEV1 | Controller down, or > 50% of in-flight downloads stuck > 30 min |
| SEV2 | Controller degraded (p95 > 1s for ≥ 10 min) OR replication entirely halted |
| SEV3 | A single feature degraded, no user-facing data loss |

## Summary

Two-paragraph executive summary that a non-engineer can read.

## Impact

- How many users / tenants were affected?
- How many tasks were affected (succeeded / failed / cancelled)?
- Any data loss? If yes, full inventory.
- Did SLO miss? Reference the affected line in `docs/operator/sla-slo.md`.

## Timeline (UTC)

| Time | Event |
|------|-------|
| 13:42 | Alert fires: `dlw_controller_role{}` flapping |
| 13:43 | Pager hits on-call; ack within 60 s |
| ... | ... |
| 14:18 | Mitigated: leader pinned to pod-B |
| 14:31 | Root-caused: PG connection pool exhaustion |
| 14:45 | Restored: pool size bumped via config map |
| 15:30 | Incident closed |

## Root cause

The actual mechanism that caused the impact. Cite specific code paths
(file:line) and configuration values. Distinguish:

1. **Trigger** — what changed that day to expose the latent bug.
2. **Defect** — what's wrong with the code or design.
3. **Detection gap** — why monitoring didn't catch it earlier.

## What went well

At least 3 specific things — the on-call ack speed, the runbook step
that worked, the test that protected against worse. Don't skip this; it
informs what to keep.

## What went poorly

At least 3 specific things — what slowed mitigation, what was missing
from the runbook, what the dashboard didn't show. Be concrete.

## Action items

Track every fix in our project board, not this doc.

| ID | Description | Owner | Type | Priority | Linked PR / issue |
|----|-------------|-------|------|----------|-------------------|
| A1 | Add alert for `pg_pool_used > 80%` | @name | Detect | P1 | #1234 |
| A2 | Bump default pool size in helm chart | @name | Prevent | P0 | #1235 |
| A3 | Add chaos-drill 5 for PG pool exhaustion | @name | Verify | P2 | runbook §5 |

**Types:** Prevent (won't happen again) / Detect (will catch sooner)
/ Mitigate (will recover faster) / Verify (will know we got it right).

## Communication

Public message to users / tenant admins (paste the actual broadcast).
Internal Slack thread link.

## Related material

- Grafana snapshot link (must persist 90 days)
- Controller logs (kubectl + S3 archive path)
- Past incidents on the same surface (link 1-3 if any)
- Relevant runbook section
