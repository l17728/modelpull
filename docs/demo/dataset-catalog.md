# Demo Dataset Catalog

> Curated list of public HuggingFace models used by modelpull for demo,
> regression smoke, and (future) chunk-level benchmarks. All revisions are
> pinned to specific commit SHAs to keep CI / docs reproducible.

## 1. Phase 1 alpha demo (default)

| Field | Value |
|-------|-------|
| `repo_id` | `sentence-transformers/all-MiniLM-L6-v2` |
| `revision` | `c9745ed1d9f207416be6d2e6f8de32d1f16199bf` |
| Total size | ~91 MB |
| File count | 9 (config.json, model.safetensors, tokenizer files, pytorch_model.bin, …) |
| LFS files | Yes (model.safetensors + pytorch_model.bin) |
| License | Apache-2.0 |
| Use cases | `scripts/demo-alpha.sh` default (PR #6); manual smoke `tests/e2e/test_hf_s3_smoke_local.py` (PR #5); `dlw-seed --demo` |
| Walltime estimate | 30–90 s on 100 Mbps link |

Pin command (used during W5 implementation):

```bash
uv run python -c "from huggingface_hub import HfApi; print(HfApi().repo_info('sentence-transformers/all-MiniLM-L6-v2', revision='main').sha)"
```

## 2. Faster smoke (ad-hoc dev iteration)

| Field | Value |
|-------|-------|
| `repo_id` | `prajjwal1/bert-tiny` |
| `revision` | `6f75de8b60a9f8a2fdf7b69cbd86d9e64bcb3837` |
| Total size | ~17 MB |
| Use cases | Fast iteration on the executor pipeline; not used by default demo |

## 3. Phase 2+ (chunk-level / large-file path)

NOT downloaded in Phase 1 CI or alpha demo. Catalogued for forward planning.

| Field | Value |
|-------|-------|
| `repo_id` | `deepseek-ai/DeepSeek-V3` |
| Total size | ~689 GB / 163 files (FP8) |
| Use cases | Phase 2 W2 chunk-level multi-thread benchmark; Phase 2 §2.5 P-004 1 GB/s target |

## How to add a new entry

1. Pin the revision SHA (see Pin command above); never use `main` for catalog entries — it drifts.
2. Note license; modelpull alpha is internal but downstream redistribution implications matter.
3. If the model is gated/private, add an "Auth required" row + cross-link to Phase 2 plan (HF Token reverse-proxy).
4. Update this file in the same PR as any test / script that depends on the new entry.

## References

- Phase 1 §1.5 acceptance E2E-001 — single model HF→S3 (any from #1 or #2 satisfies)
- `scripts/demo-alpha.sh` — uses entry #1 by default; override via `DEMO_REPO_ID` env (PR #6)
- `src/dlw/fixtures.py` `ALPHA_DEMO_REPO_ID` / `ALPHA_DEMO_REVISION` — used by `dlw-seed --demo`
- `docs/demo/runbook.md` — operator-facing demo flow (PR #6)
