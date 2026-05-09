# Phase 1 Week 4 — Real HF Hub + S3 Multipart + Streaming SHA256 Design

> Spec for the production-shaped download pipeline that closes Phase 1's
> exit gate E2E-001: download a real model from HuggingFace to S3 with
> verified sha256. Replaces Week 3's MockDownloader.

- **Status**: design approved (2026-05-09)
- **Phase**: Phase 1, Week 4 (only Phase 1 week left before Week 5 dev-infra)
- **Source roadmap**: `docs/v2.0/08-mvp-roadmap.md` §1.5 (出场标准 E2E-001) + §1.6 Week 3-4 (S3 multipart + sha256 + 联调)
- **Companion**: `docs/v2.0/02-protocol.md` §3 (task API), `docs/v2.0/04-security-and-tenancy.md` §3.2 (STS — Phase 2)
- **Author**: l17728
- **Reviewer**: TBD (multi-agent reviewer pass before plan execution)

---

## 1. Goal & Non-Goals

### 1.1 Goal

Replace `MockDownloader` with a real HF→S3 streaming pipeline that satisfies
Phase 1 §1.5 exit criteria E2E-001:

> 能完成 1 个模型从 HuggingFace 到 S3 的下载，任务级最终校验比对所有 sha256。

End of Week 4: a single `POST /api/v1/tasks {repo_id, revision, storage_id}`
triggers a fully autonomous run that:

1. Controller calls HF API to enumerate the repo's files at the given revision.
2. For each file, controller creates a `FileSubTask` row with `expected_sha256`.
3. Executor `/poll`s, gets an assignment with HF URL hint + S3 storage config.
4. Executor streams bytes from `huggingface.co/.../resolve/.../{filename}` directly into S3 multipart upload (no local disk landing) while computing sha256 on the fly.
5. Executor reports `actual_sha256` to controller.
6. Controller verifies `actual_sha256 == expected_sha256`; mismatch → subtask `failed`.
7. When all subtasks succeed, parent task transitions to `succeeded` (logic already exists from W2).

### 1.2 Non-goals (deferred — explicit list)

| Item | Deferred to | Reason |
|------|-------------|--------|
| HF Token reverse-proxy (invariant 2) | Phase 2 | Phase 1 sets `DLW_EXECUTOR_HF_TOKEN` env on executor; documented violation |
| STS temporary credentials (invariant 3) | Phase 2/3 | Phase 1 uses `boto3` default credential chain (env vars on executor) |
| Multipart upload_id persistence + crash recovery | Phase 2 | DB column `multipart_upload_id` already exists; logic deferred |
| Range resume on HF stream interruption | Phase 2 | Retry restarts entire download; ROI fine for ≤3GB files |
| Chunk-level multi-threaded download (`DirectOffsetDownloader`) | Phase 2 | Single httpx stream per file is enough for Phase 1 demo |
| Multi-source / hf-mirror auto-failover | Phase 3 | Phase 1 single endpoint via `HF_ENDPOINT` env override |
| Per-tenant HF tokens / private repos | Phase 3 | Phase 1 single token, public repos default |
| `storage_backends.config_encrypted` KMS envelope encryption | Phase 3 | Phase 1 plain JSON in the column; magic-byte detection added in Phase 3 |
| Per-task storage_config override | Phase 3 | Phase 1 reads `storage_backends` row by `storage_id` |
| Pickle rejection / Sigstore verification | v2.2+ | Invariant 38 |
| LFS pointer detection | n/a | `huggingface_hub` SDK handles transparently |

---

## 2. Tech Stack Additions

| Concern | Choice | Notes |
|---------|--------|-------|
| HF API (controller) | `huggingface_hub` SDK | `HfApi.list_repo_tree()` returns `RepoFile` objects with `path`, `size`, `lfs.sha256`. Sync API; wrapped in `asyncio.to_thread`. |
| HF download (executor) | `httpx` async streaming | Already in deps. `client.stream('GET', url)` + `aiter_bytes(64KB)`. Follow redirects (HF LFS → CDN). |
| S3 client | `boto3` + `asyncio.to_thread` | Same pattern as W3 MockDownloader. Works against AWS S3 + MinIO + 阿里 OSS / 华为 OBS via `endpoint_url`. |
| S3 testing | `moto[s3]` in-process | No Docker needed (consistent with Phase 1 local-PG-no-testcontainers culture). |
| HF testing | `httpx.MockTransport` | Mock the JSON tree response + the bytes stream. |
| Local manual smoke | `minio` binary subprocess + small public model | `tests/e2e/test_hf_s3_smoke_local.py` with `@pytest.mark.manual`; not on CI. |

**No new heavy deps**. `huggingface_hub` is small (~MB); `boto3` is unavoidable for S3; `moto[s3]` is dev-only.

---

## 3. Components

### 3.1 New: `dlw.services.hf_metadata`

```python
# src/dlw/services/hf_metadata.py
@dataclass(frozen=True)
class RepoFile:
    path: str
    size: int
    sha256: str | None      # populated for LFS files; None for tiny config files

class RepoNotFound(Exception): ...
class HfPrivateOrAuthRequired(Exception): ...
class HfNetworkError(Exception): ...

async def list_repo_tree(
    repo_id: str, revision: str, *,
    hf_endpoint: str, hf_token: str | None,
) -> list[RepoFile]:
    """Wraps huggingface_hub.HfApi.list_repo_tree.

    Sync SDK call wrapped in asyncio.to_thread.
    Filters out non-file entries (directories) and the following metadata
    files (case-sensitive, only at repo root):
      .gitattributes, .gitignore, README.md, LICENSE, USAGE.md
    Everything else (config.json, tokenizer.json, *.safetensors, *.bin,
    *.onnx, *.gguf, etc.) IS downloaded. The whitelist is conservative —
    we'd rather download a small unused file than skip a needed weight.

    Raises:
      RepoNotFound       — HF 404 (repo or revision missing)
      HfPrivateOrAuthRequired — HF 401/403 (token needed)
      HfNetworkError     — connection / timeout / DNS
    """
```

### 3.2 Modified: `dlw.services.task_service`

```python
# src/dlw/services/task_service.py — drop _MOCK_FILES

async def create_task(
    session, body, *, owner_user_id, tenant_id, project_id,
    hf_endpoint: str, hf_token: str | None,    # NEW dependency-injected
) -> DownloadTask:
    files = await list_repo_tree(body.repo_id, body.revision,
                                  hf_endpoint=hf_endpoint, hf_token=hf_token)
    if not files:
        raise EmptyRepo(body.repo_id, body.revision)

    task = DownloadTask(...)  # same as before
    session.add(task); await session.flush()
    for f in files:
        session.add(FileSubTask(
            task_id=task.id, tenant_id=tenant_id,
            filename=f.path, file_size=f.size,
            expected_sha256=f.sha256, status="pending",
        ))
    await session.flush()
    return task
```

`api/tasks.py` translates `RepoNotFound`→404, `HfPrivateOrAuthRequired`→422,
`HfNetworkError`→503, `EmptyRepo`→422.

### 3.3 Modified: `dlw.services.scheduler.complete_subtask`

Insert sha256 verification before `final_status` is committed:

```python
# Inside complete_subtask, after token check, before status assignment:
if final_status == "succeeded" and sub.expected_sha256 is not None:
    if actual_sha256 != sub.expected_sha256:
        final_status = "failed"
        error = (f"sha256 mismatch: expected={sub.expected_sha256[:12]}… "
                 f"actual={(actual_sha256 or '')[:12]}…")
```

Verification is **controller-side** (not executor-side) — single source of
truth, easier to evolve in Phase 2 (e.g., add SHA512, multi-hash).

### 3.4 Replaced: `dlw.executor.downloader`

`MockDownloader` deleted. `HfS3StreamDownloader` takes its place with the
same `download(...) -> DownloadResult` interface (so `runner.py` doesn't change):

```python
@dataclass(frozen=True)
class DownloadResult:
    bytes_written: int
    actual_sha256: str
    s3_key: str          # NEW (replaces local file_path)

@dataclass(frozen=True)
class Assignment:
    """Slim payload passed from runner to downloader."""
    subtask_id: uuid.UUID
    task_id: uuid.UUID
    repo_id: str
    revision: str
    filename: str
    file_size: int | None
    storage_config: StorageConfig    # bucket / key_prefix / region / endpoint_url


class HfS3StreamDownloader:
    def __init__(self, *, settings: ExecutorSettings) -> None: ...

    async def download(self, *, assignment: Assignment) -> DownloadResult:
        """1 file = 1 HF GET stream → 1 S3 multipart upload, sha256 tee'd.

        See spec §3.5 for the full pipeline pseudocode.
        """
```

### 3.5 Pipeline pseudocode (the streaming core)

```python
async def download(self, *, assignment) -> DownloadResult:
    url = f"{self._settings.hf_endpoint}/{assignment.repo_id}/" \
          f"resolve/{assignment.revision}/{assignment.filename}"
    s3 = self._make_s3_client(assignment.storage_config)
    bucket = assignment.storage_config.bucket
    # _compose_key: storage_config.key_prefix + assignment.repo_id + "/" +
    #               assignment.revision + "/" + assignment.filename
    # Example: "phase1/deepseek-ai/DeepSeek-V3/abc123…/model.safetensors"
    # Phase 3 plan introduces tenant-aware path_template substitution
    # (e.g., {tenant}/{repo_id}/...); Phase 1 keeps the simple concat above.
    key = self._compose_key(assignment)

    headers = {}
    if self._settings.hf_token:
        headers["Authorization"] = f"Bearer {self._settings.hf_token}"

    upload_id: str | None = None
    sha = hashlib.sha256()
    bytes_total = 0
    parts: list[dict] = []
    buf = bytearray()
    part_no = 1
    PART_SIZE = self._settings.multipart_part_size_bytes  # 5 MiB

    try:
        async with httpx.AsyncClient(
            timeout=self._settings.download_timeout_seconds,
            follow_redirects=True,
        ) as hc:
            async with hc.stream('GET', url, headers=headers) as resp:
                resp.raise_for_status()       # raises on 4xx/5xx

                upload_id = await asyncio.to_thread(
                    lambda: s3.create_multipart_upload(Bucket=bucket, Key=key)['UploadId']
                )

                async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                    sha.update(chunk)
                    bytes_total += len(chunk)
                    buf.extend(chunk)
                    while len(buf) >= PART_SIZE:
                        body = bytes(buf[:PART_SIZE])
                        del buf[:PART_SIZE]
                        etag = await asyncio.to_thread(self._upload_part,
                            s3, bucket, key, upload_id, part_no, body)
                        parts.append({'PartNumber': part_no, 'ETag': etag})
                        part_no += 1

                # last (possibly < 5MB; allowed for last part only)
                if buf:
                    etag = await asyncio.to_thread(self._upload_part,
                        s3, bucket, key, upload_id, part_no, bytes(buf))
                    parts.append({'PartNumber': part_no, 'ETag': etag})

        await asyncio.to_thread(lambda: s3.complete_multipart_upload(
            Bucket=bucket, Key=key, UploadId=upload_id,
            MultipartUpload={'Parts': parts}))
        return DownloadResult(
            bytes_written=bytes_total,
            actual_sha256=sha.hexdigest(),
            s3_key=key,
        )

    except BaseException:
        if upload_id is not None:
            try:
                await asyncio.to_thread(lambda: s3.abort_multipart_upload(
                    Bucket=bucket, Key=key, UploadId=upload_id))
            except Exception as e:
                _log.warning("multipart abort failed (will be GC'd later): %s", e)
        raise
```

Key invariants:

- **Memory bounded**: O(`PART_SIZE`) ≈ 5 MiB regardless of total file size.
- **No disk landing**: stream goes HF → memory buffer → S3 directly.
- **sha256 same-source**: bytes that update the hash are the bytes uploaded.
- **Abort on any failure**: `BaseException` (covers `asyncio.CancelledError` too — let it propagate after abort, matching W3-A shutdown discipline).

---

## 4. Configuration & Credentials

### 4.1 Controller config additions (`src/dlw/config.py`)

```python
class Settings(BaseSettings):
    # ... existing ...
    hf_endpoint: str = "https://huggingface.co"
    hf_token: str | None = None        # used by controller's HF metadata calls
```

### 4.2 Executor config additions (`src/dlw/executor/config.py`)

```python
class ExecutorSettings(BaseSettings):
    # ... existing ...
    hf_endpoint: str = "https://huggingface.co"
    hf_token: str | None = None
    s3_region: str = "us-east-1"
    s3_endpoint_url: str | None = None     # MinIO/S3-compatible; None → AWS
    s3_path_style: bool = True             # MinIO needs path-style; AWS works either
    multipart_part_size_bytes: int = 5 * 1024 * 1024
    download_timeout_seconds: int = 300
```

### 4.3 `StorageBackend.config_encrypted` Phase 1 schema

```jsonc
{
  "bucket": "modelpull-dev",
  "region": "us-east-1",
  "endpoint_url": "http://minio:9000",   // optional; null for AWS
  "key_prefix": "phase1/"                // optional; "" for bucket root
}
```

Phase 1 stores plain JSON bytes (column type `LargeBinary`). Phase 3 plan
introduces magic-byte detection for envelope encryption — backwards-compat.

### 4.4 New schema: `StorageConfig` (DTO)

```python
# src/dlw/schemas/storage.py — NEW
class StorageConfig(BaseModel):
    """Decrypted Phase 1 storage backend config — embedded in /poll response."""
    bucket: str
    region: str = "us-east-1"
    endpoint_url: str | None = None
    key_prefix: str = ""
```

### 4.5 `AssignmentResponse` extension

```python
# src/dlw/schemas/executor.py — modify
class AssignmentResponse(BaseModel):
    subtask_id: uuid.UUID
    task_id: uuid.UUID
    filename: str
    file_size: int | None
    expected_sha256: str | None
    assignment_token: uuid.UUID
    # NEW:
    repo_id: str
    revision: str
    storage_config: StorageConfig
```

`api/executors.py` `/poll` handler joins `FileSubTask → DownloadTask → StorageBackend`,
parses `config_encrypted` JSON, returns the augmented response.

`src/dlw/executor/client.py` `ControllerClient.poll()` parses the response
into the extended `AssignmentResponse` Pydantic model — Pydantic ignores
unknown fields by default, so existing executor code keeps working until
`runner.py` is updated to forward the new fields to `HfS3StreamDownloader`.
Plan task ordering ensures the schema extension lands before the executor
download pipeline is rewritten.

### 4.6 New DB column: `file_subtasks.s3_key`

```sql
ALTER TABLE file_subtasks ADD COLUMN s3_key VARCHAR(1024) NULL;
```

Populated by controller in `complete_subtask` (executor reports it via the
report endpoint). Used for debugging + Phase 2 multipart resume.

`SubTaskReport` schema gets a new optional field:

```python
class SubTaskReport(BaseModel):
    # ... existing ...
    s3_key: str | None = Field(default=None, max_length=1024)
```

Migration: alembic autogenerate — single new column, nullable, no data backfill.

---

## 5. Error Handling Matrix

| Source | Trigger | Handling | Subtask report status |
|--------|---------|----------|-----------------------|
| HF 404 | repo / revision / file missing | no retry; `failed` (error includes URL fragment) | `failed` |
| HF 401/403 | private model, token missing or wrong | no retry; `failed` | `failed` |
| HF 410 / 451 | gone / unavailable for legal reasons | no retry; `failed` | `failed` |
| HF 429 | rate limit | tenacity retry × 3, exp backoff (1, 2, 4s) | retry-exhausted → `failed` |
| HF 5xx | transient | tenacity retry × 3 | retry-exhausted → `failed` |
| HF network (`RemoteProtocolError`, `ConnectError`, `ReadTimeout`) | mid-stream drop | tenacity retry × 3 (re-runs entire download — Phase 1 has no resume) | retry-exhausted → `failed` |
| S3 `NoSuchBucket` / `AccessDenied` | config bug | abort multipart; no retry; `failed` | `failed` |
| S3 `SlowDown` / 5xx | transient | botocore default retry × 3 (built-in) | retry-exhausted → abort + `failed` |
| sha256 mismatch | computed `actual` ≠ `expected_sha256` | controller-side detection in `complete_subtask`; flips `final_status` to `failed`. Executor first reports `succeeded` because executor doesn't see `expected_sha256`; flip is on controller only | controller flips to `failed` |
| `asyncio.CancelledError` | runner shutdown via SIGTERM | propagate after `abort_multipart`; subtask stays `assigned` (Phase 2 reclaims) | n/a — no report sent |
| Empty repo (HF returns 0 files) | repo has no files at revision | `EmptyRepo` raised in `task_service.create_task` → API returns 422 | task creation fails (no DB writes) |

---

## 6. Testing Strategy

### 6.1 Unit + integration (CI required)

```
tests/services/test_hf_metadata.py             [NEW]
  - mock huggingface_hub.HfApi.list_repo_tree via monkeypatch
  - test 404 / 401 / network mapping
  - test metadata-file filter (.gitattributes etc.)

tests/services/test_task_service.py            [MODIFY]
  - drop _MOCK_FILES tests; replace with mock list_repo_tree
  - assert FileSubTask rows match HF response (filenames + sizes + sha256)
  - assert 404/422/503 propagation

tests/services/test_scheduler.py               [MODIFY]
  - test_complete_subtask_marks_failed_on_sha_mismatch
  - test_complete_subtask_succeeds_when_sha_matches
  - test_complete_subtask_succeeds_when_expected_is_null

tests/executor/test_downloader.py              [REWRITE]
  - moto[s3] server fixture (mock_aws context manager)
  - httpx MockTransport HF mock
  - test_streams_hf_to_s3_full_pipeline
  - test_computes_sha256_during_stream (compare hashlib of source bytes)
  - test_5mb_part_boundary_2_parts (6MB body → 2 parts)
  - test_small_last_part (7MB body → 5MB + 2MB)
  - test_aborts_multipart_on_hf_error (MockTransport raises mid-stream)
  - test_aborts_multipart_on_s3_error (moto returns 500 on upload_part — use moto's set_initial_no_auth_action_count or fault injection)
  - test_retries_on_hf_429

tests/e2e/test_executor_e2e.py                 [MODIFY]
  - replace MockDownloader with HfS3StreamDownloader
  - moto[s3] server fixture
  - httpx MockTransport on HF endpoint (mock 2-file repo: config.json 4KB + tiny.bin 64KB)
  - assert moto bucket has both objects with correct bytes
  - assert task.status == "succeeded"
```

### 6.2 Manual smoke (not on CI)

```
tests/e2e/test_hf_s3_smoke_local.py            [NEW, @pytest.mark.manual]
  test_real_hf_to_minio:
    - boots minio binary on :9000 (subprocess.Popen, kill in fixture teardown)
    - creates bucket via boto3
    - repo_id = "sentence-transformers/all-MiniLM-L6-v2"
    - revision pinned to a specific commit (record SHA)
    - run controller in-process + executor; wait for task succeeded
    - assert all S3 objects exist + sha256 matches HF reported sha
    - cleanup: drop minio data dir
```

Skipped by default. Run with `pytest -m manual tests/e2e/test_hf_s3_smoke_local.py`.

### 6.3 No new CI jobs

Existing `pytest` job picks up new tests automatically. `moto[s3]` is added
to `[dependency-groups].dev` so `uv sync --all-groups` installs it.

---

## 7. Demo Workflow (`docker-compose.dev.yml` + README)

### 7.1 docker-compose.dev.yml additions

```yaml
services:
  postgres: ...                  # existing
  controller: ...                # existing — env additions: DLW_HF_ENDPOINT, optional DLW_HF_TOKEN
  executor:                      # existing — env additions:
    environment:
      AWS_ACCESS_KEY_ID: minioadmin
      AWS_SECRET_ACCESS_KEY: minioadmin
      AWS_S3_ENDPOINT_URL: http://minio:9000
      DLW_EXECUTOR_HF_ENDPOINT: https://huggingface.co     # or hf-mirror.com
      DLW_EXECUTOR_HF_TOKEN: ${DLW_EXECUTOR_HF_TOKEN:-}    # optional
    depends_on:
      controller: { condition: service_healthy }
      minio:      { condition: service_healthy }

  minio:                         # NEW
    image: minio/minio:latest
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: minioadmin
      MINIO_ROOT_PASSWORD: minioadmin
    ports: ["9000:9000", "9001:9001"]
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9000/minio/health/ready"]
      interval: 5s
      timeout: 3s
      retries: 10

  init-bucket:                   # NEW one-shot
    image: minio/mc:latest
    depends_on:
      minio: { condition: service_healthy }
    entrypoint: >-
      /bin/sh -c "
      set -e;
      mc alias set local http://minio:9000 minioadmin minioadmin;
      mc mb -p local/modelpull-dev || true;
      "
    restart: "no"
```

`storage_backends` row 1 needs to be seeded with the matching JSON in `config_encrypted` (a one-shot init script or a pytest-style seed). Phase 1 README will document a `bootstrap.sql` snippet.

### 7.2 README Week 4 demo block

(See plan; uses 4-backtick outer fence per W4-J discipline.)

---

## 8. Acceptance Criteria

- [ ] `POST /api/v1/tasks {repo_id: "<small public model>", revision: "<sha>", storage_id: 1}` creates a task with N subtasks (N = number of files in the HF repo at that revision), each with `expected_sha256` populated for LFS files.
- [ ] Executor running with `AWS_*` env + `DLW_EXECUTOR_HF_ENDPOINT` env streams every subtask file from HF to S3 (MinIO) without writing to local disk.
- [ ] sha256 computed during stream matches HF's reported sha (asserted via controller-side verification in `complete_subtask`).
- [ ] All subtasks succeeded → parent task status flips to `succeeded`.
- [ ] If any subtask sha mismatch is injected (test fixture), parent task status flips to `failed` with descriptive `error_message`.
- [ ] Existing pytest suite + new test classes all green; CI 12/12 still passes.
- [ ] Manual smoke `tests/e2e/test_hf_s3_smoke_local.py` passes locally against a small public HF model + local minio binary.
- [ ] `docker compose -f docker-compose.dev.yml up -d --build` boots PG + controller + executor + minio + init-bucket; demo workflow (POST + watch) ends with files in MinIO.
- [ ] No new files outside the cells listed in Plan §"File Structure".

---

## 9. Phase 1 Exit Gate Coverage

| §1.5 criterion | This spec |
|----------------|-----------|
| E2E-001: 1 model HF→S3 download | §1.1, §3.5, §6.2 manual smoke |
| 任务状态机所有合法 transition 单测通过 | W2 covers pending/assigned/succeeded/failed; §3.3 adds the new sha-mismatch → failed path with new tests in §6.1 |
| 任务级最终校验比对所有 sha256 (U-VER-001..003) | §3.3 controller-side verification — new tests `test_complete_subtask_marks_failed_on_sha_mismatch` etc. |
| DB schema migration alembic 支持 | §4.6 single new column (`s3_key`) — alembic autogen |
| 单测覆盖率 ≥ 80% | §6.1 — pytest job already enforces; new tests keep coverage on the rewritten downloader |
| 集成测试 I-CE-001..010 通过 | Existing W2 controller-side tests + §6.1 new HfS3StreamDownloader tests + modified e2e test |
| 无 high/critical 安全扫描发现 | gitleaks runs on every PR; no secrets in spec |
| OpenAPI 实际 yaml 与代码一致 | `api/openapi.yaml` to be updated in plan with new `storage_config` field on `AssignmentResponse` |

After Week 4 merges, Phase 1 is feature-complete. Week 5 (dev infra / pagination)
and Week 6 (alpha demo + buffer) are still on the roadmap but optional for
"Phase 1 done" — Week 4 already satisfies the §1.5 exit gate.

---

## 10. Implementation Phasing (preview for plan)

| Milestone | Deliverable | Verification |
|-----------|-------------|--------------|
| M1: HF metadata + task_service rewrite | `list_repo_tree` + `create_task` calls real HF | pytest with mocked HF |
| M2: Storage config + AssignmentResponse extension + alembic migration | `/poll` returns storage_config; `s3_key` column exists | `alembic upgrade head` is no-op idempotent; pytest |
| M3: HfS3StreamDownloader (single file pipeline) | Replace MockDownloader; runner uses new interface | pytest with moto[s3] + httpx MockTransport |
| M4: Controller-side sha256 verification | `complete_subtask` flips to `failed` on mismatch | scheduler tests |
| M5: docker-compose minio + README + manual smoke | `docker compose up` + README demo runs | manual smoke against `all-MiniLM-L6-v2` |

Plan task count: ~15-17 tasks (vs Week 3 UI's 12). Largest of Phase 1.

---

## 11. References

- Companion full design: `docs/v2.0/02-protocol.md` §3, `docs/v2.0/04-security-and-tenancy.md` §3
- Phase 1 scope: `docs/v2.0/08-mvp-roadmap.md` §1.5 / §1.6
- Precedent specs: `docs/superpowers/specs/2026-05-08-week-3-ui-scaffold-design.md`
- Precedent plans: `docs/superpowers/plans/2026-05-09-phase-1-week-3-executor-process.md`
- Existing pipeline (to replace): `src/dlw/executor/downloader.py` (`MockDownloader`)
- Existing scheduler (to extend): `src/dlw/services/scheduler.py` (`complete_subtask`)
- Existing schemas (to extend): `src/dlw/schemas/{task,subtask,executor}.py`
