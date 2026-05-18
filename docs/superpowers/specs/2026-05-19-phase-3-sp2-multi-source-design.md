# Phase 3 SP2 — Multi-Source (SourceDriver + NameResolver + speed-test + LPT/chunk routing) Design

> **Status:** Draft (brainstormed 2026-05-19).
> **Companion plan:** `docs/superpowers/plans/2026-05-19-phase-3-sp2-multi-source.md` (writing-plans, after spec approval).
> **Roadmap source:** `docs/v2.0/08-mvp-roadmap.md` §3 Phase 3 Week 2 ("多源"): SourceDriver 抽象 + HF + hf-mirror; ModelScope + NameResolver; 测速 + LPT routing; chunk-level routing + 局部重平衡. §3.5 exit: `U-SRC-*`/`I-SRC-*`, `E2E-002` (多源 auto_balance), 多源测速 5×4 ≤8s, LPT 加速 ≥2x.
> **Phase 3 decomposition:** SP1 multi-tenancy = merged (PR #15, `fa08e6d`). **This is SP2 (2nd of 4).** SP3 incremental download, SP4 CLI/SDK follow.
> **Design source:** `docs/v2.0/06-platform-and-ecosystem.md` §1 (multi-source, the authoritative section).
> **Invariant source:** `docs/v2.0/INVARIANTS.md` rows 11 (HF=SHA256 truth), 12 (cross-source verify→24h blacklist), 13 (HF unavailable→default refuse). Reuses Invariant 8 (tenant scoping, SP1).
> **Closes:** the v2.0 multi-source baseline (§1.1–§1.9 IN-scope subset). Phase-B adaptive optimizer (§1.8 cont.) stays v2.1 per the doc's own framing.

> **⚠️ Scope decisions (authoritative — supersede any broader reading of doc 06 §1):**
> 1. **Drivers: `huggingface`, `hf_mirror`, `modelscope` only.** wisemodel/opencsg/s3_mirror + the §1.10 plugin loader are **deferred** (⚙️ default-off in the doc).
> 2. **LPT = "size-descending greedy heuristic"**, NOT a bounded-optimal algorithm (doc OR-V21-04). No LP-relaxation slow-path (v2.1).
> 3. **Rebalance is minimal**: a leader-gated 60s loop reassigns a degraded source's *pending* (not-started) chunks to healthy sources. Skipped-source *recovery* re-admission and Phase-B continuous LP recalibration are **deferred** (v2.1, doc §1.8 cont.).
> 4. **SHA256 authority = HF only** (INVARIANT 11/12/13). `trust_non_hf_sha256` is honored as a task flag (HF-down → still `paused_external` unless set); its **admin approval workflow is deferred to Phase 4** (doc 04 §5/§8).
> 5. **Out:** UI source-allocation view (§1.11, frontend), incremental download (§2 = SP3), CLI/SDK (§5 = SP4), webhook/MLflow/operator/HF-cache (§4, Phase 4), refcount/global-dedup (§3.1, SP3), cost-knob optimizer (`estimate_cost` stays on the Protocol, unused by SP2).
> The companion plan embeds all of these; it is the execution source of truth.

---

## 1. Goal & Scope

### 1.1 Goal

Make a task downloadable from multiple mirror sources in parallel, choosing the fastest combination per the executing fleet, while HuggingFace remains the cryptographic source of truth.

**Mechanism.** Today a task is single-source HF: `dlw.services.hf_metadata.list_repo_tree(repo_id, revision)` enumerates files controller-side; the executor streams every file's bytes through the W3b reverse-proxy `GET /api/v1/hf-proxy/subtask/{id}` (which reconstructs the HF URL from `task.repo_id/revision + sub.filename` and injects the HF token). SP2 introduces a `SourceDriver` abstraction (the existing HF path becomes the `huggingface` driver), a `NameResolver` (the same model has different IDs per source), a **task `scheduling` phase** that resolves manifests across enabled sources, speed-probes each `(executor, source)`, computes a Longest-Processing-Time greedy file→source assignment (plus chunk-level split for big files), and a generalized `/api/v1/source-proxy` that streams each subtask/chunk from its assigned source's driver with that source's controller-side credential. After download the controller verifies every file's sha256 against HF's authoritative value; a mismatch blacklists `(source, repo, filename)` for 24h and re-fetches HF-only.

After SP2, a single-source-HF deployment still works: with only `huggingface` enabled (or only one source covering a repo), the scheduler degrades to single-source and behavior is identical to today.

### 1.2 In scope

| Item | Where |
|---|---|
| `SourceDriver` Protocol + `SourceManifest`/`SourceFile`/`SourceToken`/`SourceHealth` | `src/dlw/sources/base.py` (new) |
| `huggingface` driver (refactor existing HF enumerate+stream behind the Protocol) | `src/dlw/sources/huggingface.py` (new); `hf_metadata.py` reused internally |
| `hf_mirror` driver (HF-compatible, base-URL swap, no token, auto-skip gated) | `src/dlw/sources/hf_mirror.py` (new) |
| `modelscope` driver (own API + name mapping; no sha256) | `src/dlw/sources/modelscope.py` (new) |
| Source registry — load `config/sources.yaml` → enabled `{id: driver}` | `src/dlw/sources/registry.py` (new) |
| `NameResolver` 3-tier (identity-orgs / alias-rules / API-search, 24h cache) | `src/dlw/sources/name_resolver.py` (new); `config/resolver-rules.yaml` (new) |
| Scheduling-phase planner: resolve→probe→LPT/chunk-plan→persist | `src/dlw/services/source_scheduler.py` (new) |
| Speed probe + EWMA fusion + optimal-combo selection | `src/dlw/services/source_speed.py` (new) |
| Blacklist (5xx degrade, sha-mismatch 24h, health-timeout) | `src/dlw/services/source_blacklist.py` (new) |
| Generalized multi-source reverse-proxy | `src/dlw/api/source_proxy.py` (new) |
| `SubtaskChunk` + `SourceSpeedSample` + `SourceBlacklist` models + 1 migration | `src/dlw/db/models/source.py` (new), `src/dlw/alembic/versions/<rev>_p3sp2_multi_source.py` (new) |
| `download_tasks.source_strategy`/`source_blacklist`/`trust_non_hf_sha256`; `file_subtasks.source_id` | same migration |
| Task scheduling-phase wiring + chunk-aware claim + HF-sha256 authority gate | `src/dlw/services/task_service.py`, `src/dlw/services/scheduler.py` |
| Executor chunk downloader → `/source-proxy`, per-chunk range | `src/dlw/executor/chunk_downloader.py`, `src/dlw/executor/client.py` |
| Lifespan: unconditional registry/resolver bootstrap + leader-gated rebalance loop | `src/dlw/main.py` |
| Config: source/probe/blacklist/rebalance settings + yaml paths | `src/dlw/config.py` |
| `source_strategy`/`source_blacklist`/`trust_non_hf_sha256` on `TaskCreate` | `src/dlw/api/tasks.py`, `src/dlw/schemas/task.py` |
| Operator note: sources.yaml / resolver-rules.yaml / multi-source ops | `docs/operator/multi-source.md` (new) |

### 1.3 Non-goals (deferred — explicit)

| Item | Where |
|---|---|
| wisemodel / opencsg / s3_mirror drivers + §1.10 plugin loader | v2.1+ (⚙️ default-off in doc 06 §1.2) |
| Phase-B continuous LP recalibration; skipped-source recovery re-admission | v2.1 (doc 06 §1.8 cont. — "v2.0 反应式简化版... v2.1 升级"); SP2 keeps only the minimal degraded→reassign-pending rebalance |
| `trust_non_hf_sha256` admin approval workflow | Phase 4 (doc 04 §5/§8) — SP2 honors the boolean only |
| UI source-allocation view (§1.11) | frontend sub-project |
| Incremental/diff download (§2), global dedup/refcount (§3.1) | **SP3** |
| CLI `dlw` / Python SDK (§5) | **SP4** |
| Webhook / MLflow / K8s Operator / HF-cache (§4) | Phase 4 |
| `estimate_cost` cost-knob optimization (05 §8) | Protocol method exists; no optimizer in SP2 |
| BLAKE3 streaming hash for multi-source chunk mode | v2.2 (doc §9) — SP2 uses whole-file SHA256 re-scan after chunk merge |

---

## 2. Tech Stack Additions

| Dep | Why | Notes |
|---|---|---|
| `modelscope` SDK — **NOT added** | ModelScope driver uses raw `httpx` against its documented REST API (doc §1.9.3) | avoids a heavy SDK dep; `httpx` already present |
| `pyyaml>=6,<7` (runtime) | parse `sources.yaml` / `resolver-rules.yaml` | small, ubiquitous; not currently a direct dep — add to `pyproject.toml` + `uv lock` |

Reused (no new dep): `huggingface_hub` (HF + hf_mirror drivers — hf_mirror = `HfApi(endpoint="https://hf-mirror.com")`), `httpx` (ModelScope + proxy + probe), SQLAlchemy async, FastAPI, structlog, SP1's `Principal`/`require_perm`/`tenant_filtered`/casbin.

**One alembic migration**, `down_revision = "a4bed702cdb3"` (SP1 head). No new CI jobs. The real CI gates (verified in SP1): `pytest` (`uv sync --all-groups`, uv 0.11.9), `invariant_lint` (`tools/lint_invariants.py` AST-scans `api/tasks.py`/`services/task_service.py`/`services/scheduler.py` for invalid status literals — SP2 adds the `scheduling` task status and `subtask_chunks` statuses; **`"scheduling"` MUST be added to `tools/lint_invariants.py`'s `VALID_TASK_STATUS`, and chunk-status literals must live in `source_scheduler.py`/`source_proxy.py` which are NOT scanned, OR a chunk-status set added — confirm by running `python tools/lint_invariants.py`**), `openapi` (spectral `--fail-severity=error` + swagger-cli), `yamllint` (`api/` + note: `config/*.yaml` is **not** in the yamllint scan path `deploy/ api/`, so sources.yaml/resolver-rules.yaml are not CI-yamllinted — keep them valid anyway). `ruff`/`mypy` are local-only (not CI).

---

## 3. Components

### 3.1 `src/dlw/sources/base.py` — the Protocol

```python
class SourceDriver(Protocol):
    id: str
    domain: str
    provides_sha256: bool   # True only for huggingface / hf_mirror

    async def resolve(self, repo_id: str, revision: str
                      ) -> SourceManifest | None: ...
    async def download_range(self, file: SourceFile,
                             byte_range: tuple[int, int] | None
                             ) -> AsyncIterator[bytes]: ...
    async def health_check(self) -> SourceHealth: ...
    def estimate_cost(self, n_bytes: int, region: str) -> Decimal: ...
```

`SourceManifest(source_id, repo_id_in_source, revision_in_source, files: list[SourceFile], has_lfs_sha256)`; `SourceFile(filename, size, sha256, download_ref)` (`filename` normalized to HF-style path so cross-source files key identically); `SourceHealth(ok: bool, latency_ms: float)`; `SourceToken` (opaque per-source cred handle, resolved controller-side — never serialized to the executor; INVARIANT 2). `resolve()` returns `None` if the source doesn't cover `(repo, revision)` — not an error.

### 3.2 Drivers (`src/dlw/sources/{huggingface,hf_mirror,modelscope}.py`)

- **`huggingface`**: `resolve` wraps the existing `hf_metadata.list_repo_tree` (reused, not rewritten) → `SourceManifest(has_lfs_sha256 from LFS sha)`. `download_range` builds `{hf_endpoint}/{repo}/resolve/{rev}/{filename}` with Range + `Authorization: Bearer <tenant hf_token>` (per-tenant via `task.tenant_id` — SP1 made this real). `provides_sha256=True`.
- **`hf_mirror`**: identical protocol, `endpoint="https://hf-mirror.com"`, **no token**; if `resolve` hits 401/403 (gated) → return `None` (auto-skip, doc §1.9.2). `provides_sha256=True` (mirror passes HF LFS sha).
- **`modelscope`**: raw `httpx`. `resolve` → `GET {base}/api/v1/models/{ms_repo}/repo?Revision={rev}` (`ms_repo` from NameResolver); files have **no sha256** (`provides_sha256=False`, `SourceFile.sha256=None`). `download_range` → `GET {base}/api/v1/models/{ms_repo}/repo?Revision={rev}&FilePath={filename}` + Range.

### 3.3 `registry.py` + `config/sources.yaml`

`load_registry(path) -> SourceRegistry` parses the doc §1.12 `sources.yaml` (subset: `id/enabled/driver/config/cost_per_gb_egress`), instantiates only `enabled` drivers among the 3 supported, exposes `enabled_ids()`, `get(id)`, and `regional_defaults`. Bootstrapped **unconditionally** in `main.py` lifespan into `app.state.source_registry` (mirrors SP1's `app.state.casbin`/`settings` — the SP1 final review proved state used by request paths MUST be set in lifespan, not only test fixtures; SP2 adds a `test_lifespan_state`-style assertion).

### 3.4 `name_resolver.py` + `config/resolver-rules.yaml`

`NameResolver.resolve(source_id, hf_repo_id) -> str | None`: (1) identity if org ∈ `identity_organizations` or `source_id == huggingface`; (2) `aliases`/`per_model_overrides` rules (org swap + `transform` template, doc §1.5); (3) source search-API fallback, result cached 24h (in-memory TTL dict; persistence deferred). Miss → `None` → that source skipped for this repo (not fatal). Bootstrapped into `app.state.name_resolver` alongside the registry.

### 3.5 `services/source_scheduler.py` — the planner (task `scheduling` phase)

**Trigger:** a **leader-gated scheduling loop** (new, reusing SP1's `_quota_loop`/sweep leader-gating in `main.py` — runs only on the active controller) polls `pending` tasks, transitions `pending → scheduling`, calls `plan_task_sources`, then moves the task into the existing claimable state (or `paused_external` on the HF-authority gate). `create_task` stays fast (no inline resolve/probe). `async def plan_task_sources(session, task) -> None`:

1. **Resolve**: for each enabled source permitted by `task.source_strategy`/`source_blacklist` (and tenant policy), `NameResolver` → source repo id → `driver.resolve()`. Collect `{source_id: SourceManifest}`. **HF authority gate**: if `huggingface` manifest absent/unreachable and `not task.trust_non_hf_sha256` → task → `paused_external`, `last_error="no_sha256_authority"` (INVARIANT 13), return.
2. **Probe**: `source_speed.probe_matrix(eligible_executors, candidate_sources, a real ~probe_size_mb file)` → `{(exec,src): bytes/s}`, fused with `source_speed_samples` EWMA (α=0.3, live weight 0.7). Soft-deadline `probe_timeout_s`.
3. **Combine**: `_solve_optimal_combination` (doc §1.8) — evaluate fastest-K subsets with a +2%/extra-source overhead penalty; pick min-ETA combo.
4. **Assign**: `assign_files_lpt(files, combo_speeds)` (size-desc greedy, doc §1.6) sets `file_subtasks.source_id`. For each file `≥ chunk_level_min_file_mb` with ≥2 covering sources: split into `subtask_chunks` rows (`byte_start/byte_end/source_id/status='pending'`, speed-proportional, chunk-size-aligned) and mark the subtask `is_chunked`.
5. Persist; task → the existing claimable state. Files whose only source is non-HF and HF lacks sha256 → flagged "no multi-source acceleration" (single-source HF, doc §1.13 risk 1).

### 3.6 `api/source_proxy.py` — generalized reverse-proxy

`GET /api/v1/source-proxy/subtask/{subtask_id}` (+ `Range`): same W3b ownership chain (`require_executor_jwt` + assignment_token + epoch + confused-deputy guard — copied from `hf_proxy.py`), then: load `sub.source_id` (or, for a chunked subtask, the chunk's `source_id` from the `Range`→chunk lookup), get the driver from `app.state.source_registry`, resolve that source's controller-side token, `async for bytes in driver.download_range(file, range)` → `StreamingResponse` (header allowlist preserved). The W3b `/hf-proxy` route is **kept** (back-compat for any single-source path) but task downloads now target `/source-proxy`. INVARIANT 2: the source token never leaves the controller.

### 3.7 Blacklist & failure (`services/source_blacklist.py`)

`SourceBlacklist` table `(scope, source_id, repo_id, filename, until, reason)`. Transitions (doc §1.7): 5xx×3 on `(executor,source)` → degraded 5min (exp→30min, in-memory + sample table); sha256 mismatch on completion → `(source_id, repo_id, filename)` row, `until=now+24h`, that file re-planned HF-only; `health_check` >30s → source globally degraded until next OK probe. The scheduler/proxy consult the blacklist before assigning/streaming.

### 3.8 `main.py` lifespan + leader-gated rebalance

Unconditional (with SP1's settings/casbin block): `app.state.source_registry = load_registry(...)`, `app.state.name_resolver = NameResolver(...)`. Leader-gated (extend SP1's `_quota_loop` pattern with **two** holders — `scheduling_task_holder`, `rebalance_task_holder` — same `_on_active`/`_on_step_down` cancel wiring): `_scheduling_loop` (drives `pending → scheduling → plan_task_sources →` claimable, §3.5) and `_rebalance_loop` every `rebalance_interval_seconds` — for `downloading` tasks, detect degraded `(exec,source)` (probe<30% of plan, or 5xx-degraded), `UPDATE subtask_chunks SET source_id=<healthy>, status='pending' WHERE source_id=<degraded> AND status='pending'` via `FOR UPDATE SKIP LOCKED` (doc §1.8). In-flight chunks not interrupted; skipped-source recovery deferred.

### 3.9 Config (`config.py`)

```python
    # Phase 3 SP2 — multi-source
    sources_yaml_path: str = Field(default="config/sources.yaml")
    resolver_rules_path: str = Field(default="config/resolver-rules.yaml")
    probe_size_mb: int = Field(default=32, ge=1, le=256)
    probe_timeout_s: float = Field(default=8.0, ge=1.0, le=60.0)
    probe_history_weight: float = Field(default=0.3, ge=0.0, le=1.0)
    combo_overhead_per_source_pct: float = Field(default=2.0, ge=0.0, le=50.0)
    chunk_level_min_file_mb: int = Field(default=100, ge=1)
    speed_ewma_alpha: float = Field(default=0.3, ge=0.0, le=1.0)
    blacklist_5xx_count: int = Field(default=3, ge=1)
    blacklist_minutes: int = Field(default=5, ge=1)
    blacklist_max_minutes: int = Field(default=30, ge=1)
    sha_mismatch_blacklist_hours: int = Field(default=24, ge=1)
    rebalance_interval_seconds: float = Field(default=60.0, ge=5.0, le=600.0)
    degradation_trigger_threshold: float = Field(default=0.3, ge=0.0, le=1.0)
```

---

## 4. Approaches Considered

- **A — Driver-registry + scheduling-phase planner (chosen).** Central controller plans source→file/chunk at task `scheduling`; the generalized proxy streams per-assignment. Existing HF path becomes one driver (smallest blast radius); each unit (driver, resolver, planner, proxy, blacklist) testable in isolation with `httpx.MockTransport`/fake drivers; INVARIANT 2/11/13 each enforced in exactly one place.
- **B — Executor-side source selection.** Executor probes/picks its own source per file. Less controller coordination but: breaks central LPT/quota/blacklist consistency, and the executor would need source credentials → violates INVARIANT 2. Rejected.
- **C — Literal full doc §1 (6 drivers + plugin loader + Phase-B LP optimizer).** Matches the doc verbatim but is 2–3 milestones, most of it explicitly v2.1 in the doc itself. Rejected (YAGNI/scope; §1.3 defers it).

---

## 5. Schema Changes

One migration `<rev>_p3sp2_multi_source`, `down_revision = "a4bed702cdb3"`.

**Altered:**
```sql
ALTER TABLE download_tasks ADD COLUMN source_strategy VARCHAR(32) NOT NULL DEFAULT 'auto_balance';
ALTER TABLE download_tasks ADD COLUMN source_blacklist JSONB NOT NULL DEFAULT '[]';
ALTER TABLE download_tasks ADD COLUMN trust_non_hf_sha256 BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE file_subtasks  ADD COLUMN source_id VARCHAR(32);            -- nullable: filled at scheduling
ALTER TABLE file_subtasks  ADD COLUMN is_chunked BOOLEAN NOT NULL DEFAULT FALSE;
```
**New tables** (doc §1.4/§1.7):
```sql
CREATE TABLE subtask_chunks (
  id BIGSERIAL PRIMARY KEY,
  subtask_id UUID NOT NULL REFERENCES file_subtasks(id) ON DELETE CASCADE,
  chunk_index INT NOT NULL,
  byte_start BIGINT NOT NULL,
  byte_end   BIGINT NOT NULL,                 -- inclusive
  source_id  VARCHAR(32) NOT NULL,
  status     VARCHAR(16) NOT NULL,            -- pending|downloading|done|failed
  sha256_partial VARCHAR(64),
  bytes_done BIGINT NOT NULL DEFAULT 0,
  UNIQUE (subtask_id, chunk_index)
);
CREATE INDEX idx_chunk_sub_status ON subtask_chunks(subtask_id, status);
CREATE TABLE source_speed_samples (
  id BIGSERIAL PRIMARY KEY,
  executor_id VARCHAR(64) NOT NULL,
  source_id   VARCHAR(32) NOT NULL,
  measured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  bytes_per_sec FLOAT NOT NULL,
  sample_size BIGINT NOT NULL,
  is_active_probe BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX idx_speed_recent ON source_speed_samples(executor_id, source_id, measured_at DESC);
CREATE TABLE source_blacklist (
  id BIGSERIAL PRIMARY KEY,
  source_id VARCHAR(32) NOT NULL,
  repo_id   VARCHAR(256),
  filename  VARCHAR(512),
  until     TIMESTAMPTZ NOT NULL,
  reason    VARCHAR(64) NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_blacklist_lookup ON source_blacklist(source_id, repo_id, until);
```
All additive (existing rows get defaults); clean downgrade (drop new tables/columns, reverse order). Models registered in `src/dlw/db/models/__init__.py` (SP1 lesson — never `base.py`). These are quota/infra-style tables; Invariant 8 (tenant scoping) applies to *business* rows — `subtask_chunks` inherits scoping via its `file_subtasks`→`download_tasks.tenant_id` FK chain (queries go through `tenant_filtered` on the parent task, consistent with SP1); `source_speed_samples`/`source_blacklist` are operational, not tenant data (documented, like SP1's `casbin_rule`).

## 6. Wire Format Changes

- **New:** `GET /api/v1/source-proxy/subtask/{subtask_id}` (+ optional `Range`) — executor-auth (W3b chain), 200 stream / 403 `NOT_YOUR_SUBTASK` / 409 `STALE_ASSIGNMENT`|`EPOCH_MISMATCH` / 503 source unreachable / 502 `SOURCE_BLACKLISTED` (re-plan triggered). `/api/v1/hf-proxy/...` retained.
- **Changed:** `POST /api/v1/tasks` body (`TaskCreate`) gains optional `source_strategy` (default `auto_balance`; `pin_huggingface`|`pin_modelscope`|`list:...`|`fastest_only`), `source_blacklist: list[str]`, `trust_non_hf_sha256: bool`. `GET /api/v1/tasks/{id}` (`TaskDetail`) exposes `source_strategy` + per-subtask `source_id` + chunk summary. Task status domain gains `scheduling`.
- **Config:** the §3.9 `Settings` fields (all `DLW_`-prefixed). New files `config/sources.yaml`, `config/resolver-rules.yaml`.
- **OpenAPI:** `api/openapi.yaml` gains the source-proxy op + the new `TaskCreate`/`TaskDetail` fields + `scheduling` status enum value; must pass spectral `--fail-severity=error` + swagger-cli + yamllint(`api/`).

## 7. Error Handling Matrix

| Situation | Behaviour |
|---|---|
| HF unreachable, `trust_non_hf_sha256=false` | task → `paused_external`, `last_error="no_sha256_authority"` (INVARIANT 13); 5min retry |
| HF unreachable, `trust_non_hf_sha256=true` | proceed using other sources; no sha256 verification (flag honored; approval workflow Phase 4) |
| A source doesn't cover repo (`resolve→None`) / NameResolver miss | that source skipped; not fatal; logged |
| hf_mirror hits gated repo (401/403 on resolve) | hf_mirror auto-skipped (doc §1.9.2) |
| All sources probe = 0 | task → `paused_external`, 5min re-probe |
| Only 1 healthy source | single-source full-speed (v1.x behavior; no chunk split) |
| 5xx ×3 on `(executor,source)` | degraded 5min (exp→`blacklist_max_minutes`); planner avoids it |
| `health_check` > 30s | source globally degraded → fall back to HF until next OK probe |
| sha256 mismatch on completed file | `(source_id,repo_id,filename)` 24h blacklist; that file re-planned HF-only; subtask re-queued |
| Non-HF source, HF has no LFS sha256 for file | that file = single-source HF, no acceleration; UI-flag (doc §1.13 risk 1) |
| Chunked file, one chunk's source blacklisted mid-flight | rebalance loop reassigns that chunk's `pending` siblings; the failed chunk re-queued to a healthy source |
| Chunked file complete | whole-file SHA256 re-scan vs HF authority (multi-source can't stream-hash, 03 §6); mismatch → blacklist+re-plan |
| `source_strategy=pin_modelscope` but ModelScope down | task `paused_external` (respects explicit pin; no silent HF fallback) |
| Standby instance | rebalance loop not running (leader-gated); planner runs only on the active (scheduling happens on active) |
| Single-source-HF deployment (only `huggingface` enabled) | planner degrades to one source; identical to pre-SP2 behavior |

## 8. Testing Strategy

TDD throughout. Unit tests use `httpx.MockTransport` (driver HTTP) and fake `SourceDriver`s (planner/proxy) — no live mirrors.

| Area | File | Cases |
|---|---|---|
| Protocol/dataclasses | `tests/sources/test_base.py` | manifest/file normalization, SourceToken never-serialized |
| HF driver | `tests/sources/test_hf_driver.py` | resolve via mocked `list_repo_tree` / download_range Range / provides_sha256 |
| hf_mirror driver | `tests/sources/test_hf_mirror_driver.py` | base-url swap / no token / gated→None |
| ModelScope driver | `tests/sources/test_modelscope_driver.py` | API shape / name-mapped repo / sha256=None / download Range |
| Registry | `tests/sources/test_registry.py` | sources.yaml parse / only-enabled / unknown driver ignored / regional_defaults |
| NameResolver | `tests/sources/test_name_resolver.py` | identity / alias transform (`Meta-{name}`) / per-model override / search fallback+24h cache / miss→None |
| Speed | `tests/services/test_source_speed.py` | EWMA fusion / probe soft-timeout / all-zero handling |
| LPT | `tests/services/test_lpt.py` | size-desc greedy / U-SRC-005 many-small / U-SRC-006 few-large / single-source degen |
| Combo | `tests/services/test_combo.py` | slow source excluded by overhead penalty / 1-source / monotone-stop |
| Planner | `tests/services/test_source_scheduler.py` | resolve→probe→assign persists source_id/subtask_chunks / HF-absent+!trust→paused (INV 13) / chunk split ≥100MB / non-HF+no-sha→single-source |
| Blacklist | `tests/services/test_source_blacklist.py` | 5xx×3 degrade+expiry / sha-mismatch 24h / health-timeout global |
| Proxy | `tests/api/test_source_proxy.py` | routes to assigned driver / cred injected / INVARIANT-2 (no source token in any executor-bound payload) / ownership chain / blacklisted→502 |
| Sha authority | `tests/services/test_sha_authority.py` | non-HF verified vs HF / mismatch→blacklist+HF-refetch / chunked→whole-file rescan |
| Migration | `tests/db/test_p3sp2_migration.py` | up creates tables+cols / down clean / additive defaults |
| Rebalance | `tests/services/test_rebalance.py` | degraded source's pending chunks reassigned (SKIP LOCKED) / in-flight untouched / leader-gated |
| Lifespan | `tests/test_lifespan_state.py` (extend) | `app.state.source_registry`/`name_resolver` set by real lifespan (SP1 regression-class) |
| E2E-002 | `tests/e2e/test_multi_source.py` | auto_balance picks fastest, skips slow source; 5xx failover; sha-mismatch blacklists; HF-down pauses (mirrors doc §8) |

**Test infra (SP1 lessons, mandatory):** every DB test fixture uses `drop_all → create_all` (clean slate, session-DB collision avoidance) and is function-scoped where state must not bleed; `from dlw.db import models` import where `create_all` is used; new test dirs (`tests/sources/`) get `__init__.py`; the `make_app_with_state` conftest helper extended to also seed `app.state.source_registry`/`name_resolver` (so ASGI tests don't 500 — the SP1 CRITICAL-class pitfall), AND a `test_lifespan_state` assertion for the real lifespan. Subagent "pre-existing/passes-in-isolation" claims must be controller-verified.

## 9. Acceptance Criteria

- [ ] `SourceDriver` Protocol + 3 drivers (HF refactor / hf_mirror / ModelScope) with mocked-transport unit tests; `provides_sha256` correct per source.
- [ ] `NameResolver` 3-tier from `resolver-rules.yaml`; identity/alias/search+cache; miss→skip.
- [ ] Registry from `sources.yaml`; only enabled+supported drivers; bootstrapped in **lifespan** (+ `make_app_with_state` + `test_lifespan_state`).
- [ ] Scheduling-phase planner: resolve→probe→`_solve_optimal_combination`→LPT files + chunk-split ≥`chunk_level_min_file_mb`; persists `source_id`/`subtask_chunks`.
- [ ] HF sha256 authority enforced (INVARIANT 11/12/13): non-HF verified vs HF; HF-down→`paused_external` unless `trust_non_hf_sha256`; sha-mismatch→24h `(source,repo,filename)` blacklist + HF-refetch.
- [ ] `/api/v1/source-proxy` streams per assigned source, controller-side cred, INVARIANT 2 preserved; `/hf-proxy` retained.
- [ ] Chunk mode → whole-file SHA256 re-scan post-merge (03 §6).
- [ ] Minimal leader-gated rebalance: degraded source's pending chunks reassigned via SKIP LOCKED.
- [ ] One additive alembic migration (down_revision `a4bed702cdb3`); clean up/down; models in `db/models/__init__.py`; `scheduling`/chunk statuses accepted by `tools/lint_invariants.py`.
- [ ] `TaskCreate` gains `source_strategy`/`source_blacklist`/`trust_non_hf_sha256`; tenant-scoped via SP1 `require_perm`/`tenant_filtered` (no new RBAC).
- [ ] Full suite green; `invariant_lint`/`openapi`(spectral+swagger-cli)/`yamllint` CI gates green; `pyyaml` in `pyproject.toml` + `uv.lock` committed.
- [ ] `E2E-002` (`tests/e2e/test_multi_source.py`) passes; operator note written.

## 10. Implementation Phasing (preview for plan)

5 milestones, ~16–18 TDD tasks.

- **M1 — Source layer.** `base.py` Protocol+dataclasses; HF/hf_mirror/ModelScope drivers; registry + `sources.yaml`; NameResolver + `resolver-rules.yaml`; config fields; `pyyaml` dep. Pure unit (mock transport).
- **M2 — Schema + models.** migration (cols + 3 tables) + `db/models/source.py` + `__init__.py` reg + `lint_invariants` status additions + migration test.
- **M3 — Planner.** `source_speed` (probe+EWMA), `_solve_optimal_combination`, `assign_files_lpt`, chunk-split, `source_scheduler.plan_task_sources` + HF-authority gate; scheduling-phase wiring in `task_service`/`scheduler`; blacklist service.
- **M4 — Proxy + executor + lifespan.** `source_proxy.py`; `chunk_downloader`/`client` → `/source-proxy` per-chunk range; sha-authority verify (incl. chunked whole-file rescan); lifespan registry/resolver bootstrap + leader-gated `_scheduling_loop` + `_rebalance_loop`; `make_app_with_state` + `test_lifespan_state` extension.
- **M5 — E2E + docs + PR.** `tests/e2e/test_multi_source.py` (E2E-002), OpenAPI updates, `docs/operator/multi-source.md`, full suite + all CI gates, final whole-impl security/correctness review, PR, squash-merge.

Branch: `feat/phase-3-sp2-multi-source` (off `main` @ `fa08e6d`).

## 11. References

- Design: `docs/v2.0/06-platform-and-ecosystem.md` §1 (authoritative), §8 (E2E).
- Roadmap: `docs/v2.0/08-mvp-roadmap.md` §3 W2 + §3.5 exit.
- Invariants: `docs/v2.0/INVARIANTS.md` 11/12/13 (HF authority/blacklist/refuse), 8 (tenant scope, SP1).
- Current-state anchors: `src/dlw/services/hf_metadata.py` (`list_repo_tree`/`RepoFile`), `src/dlw/api/hf_proxy.py` (W3b proxy, ownership chain to copy), `src/dlw/executor/chunk_downloader.py` (range download), `src/dlw/services/scheduler.py`/`task_service.py` (status machine; `tools/lint_invariants.py` status domains), alembic head `a4bed702cdb3`.
- Predecessor: `docs/superpowers/specs/2026-05-18-phase-3-sp1-multi-tenancy-design.md` (per-tenant `task.tenant_id`, `Principal`/`require_perm`/`tenant_filtered`, lifespan-state lesson, leader-gated-loop pattern reused for rebalance).
- SP1 merged: https://github.com/l17728/modelpull/pull/15 (squash `fa08e6d`).
