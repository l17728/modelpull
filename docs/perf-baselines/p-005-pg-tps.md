# P-005 — PostgreSQL TPS Baseline

> Per `docs/v2.0/07-test-plan.md` §perf row P-005.
> Target: **5000 commits/s** sustained.
> Phase 2 W2 §2.4 entry: "Phase 1 实测 P-005 数据存在；如不达标先优化".

## Methodology

`pgbench` TPC-B-like workload on a freshly initialised DB (scale 10 ≈ 150 MB).
Run via `./scripts/bench-pg-tps.sh` — see that script for parameters + raw
output capture.

## Environment

| Field | Value |
|-------|-------|
| Date | _filled by `scripts/bench-pg-tps.sh` Step 4 / Task 4_ |
| Host | _filled — `uname -a` from script header_ |
| OS | _Windows 11 / Linux / macOS — filled at run time_ |
| CPU | _filled manually_ |
| Memory | _filled manually_ |
| Disk | _filled manually (NVMe / SATA SSD / HDD)_ |
| PG version | _filled by script (`SHOW server_version`)_ |
| PG auth | trust (dev only) |
| PG fsync / synchronous_commit | default (on / on) |

## Result

_M3 (Task 4) fills this section with the tail of `docs/perf-baselines/p-005-pg-tps.txt`:_

```
<sustained TPS line(s) from pgbench output, e.g.:
  tps = 2473.41 (without initial connection time)
  tps = 2475.83 (without initial connection time)>
```

**Sustained TPS: _filled by Task 4_ commits/s**

## Assessment vs target (5000 commits/s)

| Outcome | Action |
|---------|--------|
| ≥ 5000 | ✅ Phase 2 W2 entry met. No optimisation needed. |
| 1000–5000 | ⚠️ Marginal. Likely usable for Phase 2 single-controller — re-run with production-grade PG (managed RDS / fsync tuning) before declaring P-005 failed. Document under Phase 2 W2 entry §2.4. |
| < 1000 | ❌ Optimise first. Investigate: fsync, WAL, disk, max_connections. |

_M3 (Task 4) fills the verdict here based on actual numbers._

## What this does NOT measure

- modelpull's actual write mix (`claim_one_subtask FOR UPDATE SKIP LOCKED` + `complete_subtask`). Phase 2 W2 may add a custom `pgbench --file` script if business-workload TPS diverges from TPC-B.
- Multi-executor concurrency. Phase 2 W2's multi-executor scheduler benchmark covers this.
- Read-heavy load (Phase 1 W3 UI polling `/tasks` every 5s). P-002 covers controller API QPS.

## Reproduce

```bash
cd <repo root>
./scripts/bench-pg-tps.sh
# → tees raw output to docs/perf-baselines/p-005-pg-tps.txt (gitignored)
# → then update Environment + Result + Assessment sections in this file
```
