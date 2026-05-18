# Multi-Source Download — Operator Guide (SP2)

> **Cross-references**: `docs/v2.0/06-platform-and-ecosystem.md` §1 (design rationale,
> scheduling algorithm, name-resolver detail); `docs/v2.0/INVARIANTS.md` 11/12/13
> (SHA256 authority, cross-source verification, HF-down policy).

---

## 1. `config/sources.yaml`

Controls which download sources the controller loads at startup.

```yaml
sources:
  - id: huggingface            # unique identifier used in source_blacklist, logs, etc.
    enabled: true
    driver: huggingface        # must be a supported driver (see §1.1)
    config:
      base_url: "https://huggingface.co"
      timeout_seconds: 30
    cost_per_gb_egress: 0.09   # USD; used by cost-aware scheduling (future)

  - id: hf_mirror
    enabled: true
    driver: hf_mirror
    config:
      base_url: "https://hf-mirror.com"
      timeout_seconds: 30
    cost_per_gb_egress: 0.0

  - id: modelscope
    enabled: true
    driver: modelscope
    config:
      base_url: "https://www.modelscope.cn"
      timeout_seconds: 30
    cost_per_gb_egress: 0.0

balancing:
  speed_ewma_alpha: 0.3         # EWMA smoothing factor for speed samples
  chunk_level_min_file_mb: 100  # files smaller than this are not chunk-routed

regional_defaults:
  cn-north: ["hf_mirror", "modelscope", "huggingface"]
  us-east:  ["huggingface"]
```

### 1.1 Supported drivers (v2.0)

| Driver ID     | Endpoint              | Notes                        |
|---------------|-----------------------|------------------------------|
| `huggingface` | huggingface.co        | Authoritative SHA256 source  |
| `hf_mirror`   | hf-mirror.com         | HF-compatible reverse proxy  |
| `modelscope`  | modelscope.cn         | Requires name-resolver rules |

Entries with an unrecognised driver are logged as a warning and skipped at startup.

Drivers deferred to v2.1: `wisemodel`, `opencsg`, `s3_mirror`, `plugin`
(see §6 Deferred items).

---

## 2. `config/resolver-rules.yaml`

Maps HuggingFace repo IDs to their equivalents on other sources.

```yaml
identity_organizations:
  # Organizations whose repo IDs are identical across all sources.
  - deepseek-ai
  - Qwen
  - 01-ai
  - THUDM
  - baichuan-inc
  - mistralai

aliases:
  # Transform rules for organizations with different naming on other sources.
  - hf_org: meta-llama
    modelscope_org: LLM-Research
    transform: "Meta-{name}"        # e.g. Llama-3.1-8B → Meta-Llama-3.1-8B

per_model_overrides:
  # Exact per-model overrides; takes precedence over aliases.
  # - hf: "specific-org/specific-model"
  #   modelscope: "different-org/different-name"
```

The resolver applies three-tier lookup: (1) identity match, (2) alias rule,
(3) source search API fallback (cached 24 h). If no mapping is found the
source is skipped for that task.

---

## 3. SP2 Environment Settings (`DLW_*`)

All settings live under `SourceSettings` in `src/dlw/config.py` and are read
from environment variables with the `DLW_` prefix (or from the Helm configmap).

| Setting                           | Default | Description                                                              |
|-----------------------------------|---------|--------------------------------------------------------------------------|
| `DLW_PROBE_SIZE_MB`               | 32      | Bytes downloaded per source during the scheduling-phase speed probe      |
| `DLW_PROBE_TIMEOUT_S`             | 8.0     | Soft deadline (seconds) for each probe; partial bytes still recorded     |
| `DLW_PROBE_HISTORY_WEIGHT`        | 0.3     | EWMA history weight; live probe weight = `1 - probe_history_weight`      |
| `DLW_CHUNK_LEVEL_MIN_FILE_MB`     | 100     | Files smaller than this are not split across sources                     |
| `DLW_SHA_MISMATCH_BLACKLIST_HOURS`| 24      | Duration to blacklist a `(source, repo, filename)` after SHA256 mismatch |
| `DLW_REBALANCE_INTERVAL_SECONDS`  | 60.0    | How often the background rebalancer re-evaluates in-flight task routing  |

Tuning guidance: increase `PROBE_SIZE_MB` to 64 for large-model repos where
speed variance is high; reduce `PROBE_TIMEOUT_S` below 8 only on low-latency
networks where probes consistently finish within 3-4 s.

---

## 4. `source_strategy` Task Field

Set on `POST /api/v1/tasks` in the `source_strategy` field.

| Value            | Behaviour                                                                |
|------------------|--------------------------------------------------------------------------|
| `auto_balance`   | Default. Probe all enabled sources, allocate files/chunks by speed.      |
| `fastest_only`   | Probe all sources, use only the single fastest.                          |
| `pin_huggingface`| Skip probe; download everything from HuggingFace only.                  |
| `pin_modelscope` | Skip probe; use ModelScope only (resolver rules applied automatically).  |
| `list:a,b`       | Use only the listed source IDs (comma-separated); probe between them.    |

Sources listed in `source_blacklist` (array of source IDs) are always excluded
regardless of strategy.

---

## 5. SHA256 Authority Rules (INVARIANTS 11/12/13)

### INVARIANT 11 — HF is the authoritative SHA256 source

All files downloaded from any source must be verified against the SHA256 value
that HuggingFace provides in its LFS manifest. No other source's self-reported
SHA256 is accepted as truth.

### INVARIANT 12 — Cross-source verification is mandatory

After completing a download (single-source or chunk-level multi-source), the
controller compares the actual file SHA256 against the HF-supplied value.
A mismatch triggers a `(source_id, repo_id, filename)` blacklist for
`sha_mismatch_blacklist_hours` (default 24 h). Subsequent subtasks for that
combination fall back to HuggingFace.

### INVARIANT 13 — HF unavailable → task paused unless `trust_non_hf_sha256`

When HuggingFace is unreachable and the task was created with the default
`trust_non_hf_sha256: false`:

- The task transitions to `paused_external` with error code `no_sha256_authority`.
- No bytes are downloaded from alternative sources because integrity cannot be
  guaranteed.

Set `trust_non_hf_sha256: true` on the task to opt out of this guarantee and
allow downloads to proceed using other sources' self-reported checksums.

**Special case**: if a file has no SHA256 pinned in HF's manifest at all (rare,
typically raw text files), it is always routed exclusively through HuggingFace
regardless of strategy.

---

## 6. Scheduling and Rebalancing — Leader-Gated

The scheduling phase (task `pending` → `scheduling` → `downloading`) and the
background rebalancer both run exclusively on the **active controller** (the
current Raft/leader-election winner). Standby controllers do not run probes or
mutate source assignments.

Task state `scheduling` is transient: it covers the probe window
(`probe_timeout_s`) plus assignment computation. If the controller loses
leadership during scheduling the task reverts to `pending` and is picked up by
the new leader.

The rebalancer (interval: `rebalance_interval_seconds`) re-probes sources for
tasks whose in-flight speed deviates significantly from the initial probe, and
may reassign future chunks. It does not interrupt chunks already in flight.

Note: per-executor probing (where each executor independently probes its local
sources) is deferred to v2.1.

---

## 7. Deferred to v2.1

The following capabilities are scoped out of v2.0 and will ship in v2.1:

- `wisemodel` and `opencsg` source drivers
- `s3_mirror` driver and per-task `s3_direct_source` (schema reserved)
- Plugin-based source driver API
- Per-executor probing (executors report their own speed matrix independently)
- Automatic 5xx / health-check triggered source blacklist transitions
- Source cost accounting UI and budget enforcement
