# Phase 3 SP2 — Multi-Source Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Download a task from multiple mirror sources (HF / hf-mirror / ModelScope) in parallel, picking the fastest combination per fleet, with HuggingFace as the cryptographic source of truth.

**Architecture:** A `SourceDriver` Protocol (`dlw/sources/`) with 3 drivers behind a `sources.yaml` registry + a `NameResolver`; a leader-gated scheduling loop runs `plan_task_sources` (resolve → speed-probe → LPT/chunk plan → persist `file_subtasks.source_id`/`subtask_chunks`); a generalized `/api/v1/source-proxy` streams each subtask/chunk from its assigned source's driver with controller-side creds; HF sha256 authority + 24h blacklist on mismatch; a minimal leader-gated rebalance loop reassigns a degraded source's pending chunks.

**Tech Stack:** FastAPI + SQLAlchemy 2 async + asyncpg, `huggingface_hub` (HF + hf_mirror), raw `httpx` (ModelScope/proxy/probe), `pyyaml` (new), SP1's `Principal`/`require_perm`/`tenant_filtered`/casbin, leader-gated-loop pattern from SP1 `_quota_loop`.

**Spec:** `docs/superpowers/specs/2026-05-19-phase-3-sp2-multi-source-design.md`. **Branch:** `feat/phase-3-sp2-multi-source` (off `main` @ `fa08e6d`, spec committed `ccdb9e8`).

**Conventions (verified against the codebase — follow exactly):**
- Bash tool errors on `cd <args>` (Windows); working dir is already `D:\download_weights`. Use `uv run pytest ...` / `uv run alembic ...`.
- ORM models register in `src/dlw/db/models/__init__.py` (imports + sorted `__all__`). **Never** add model imports to `src/dlw/db/base.py` (circular import; tables won't create).
- `tools/lint_invariants.py` `VALID_TASK_STATUS` ALREADY contains `"scheduling"` (line 95) — no edit needed. It AST-scans only `src/dlw/api/tasks.py`, `services/task_service.py`, `services/scheduler.py` for task/subtask status literals. **Chunk-status literals (`pending|downloading|done|failed`) MUST live only in `source_scheduler.py`/`source_proxy.py`/`source_blacklist.py` (NOT scanned). Never put a chunk-status string literal into the 3 scanned files.**
- DB tests: `@pytest.mark.slow`, use the session `engine` fixture; **every DB fixture does `drop_all` → `create_all`** (session-DB collision avoidance — SP1 lesson) and is function-scoped unless module sharing is safe. `import dlw.db.models  # noqa: F401` before any `Base.metadata.create_all` so all tables register.
- New test dirs need an empty `__init__.py` (`tests/sources/` is new).
- API tests build the app via `tests.conftest.make_app_with_state(ephemeral_ca, enrollment_token="e")` (seeds `app.state.settings`/`casbin`; SP2 extends it to also seed `source_registry`/`name_resolver`). System-JWT auth via `tests.conftest.principal_headers(secret="unit-secret", role="tenant_admin")` with an autouse fixture setting `DLW_SYSTEM_JWT_SECRET="unit-secret"` + `get_settings.cache_clear()`.
- Real CI gates (no ruff/mypy/code-vs-yaml CI): `pytest` (`uv sync --all-groups`, uv 0.11.9), `invariant_lint` (`uv run python -m pytest tools/test_lint_invariants.py` + `python tools/lint_invariants.py` + `python tools/lint_no_direct_status_write.py`), `openapi` (`spectral lint api/openapi.yaml --fail-severity=error` + `swagger-cli validate api/openapi.yaml`), `yamllint` (`deploy/ api/` — `config/*.yaml` NOT scanned but keep valid). New deps → edit `pyproject.toml`, `uv lock`, commit `uv.lock`.
- Service layer does NOT commit; caller commits (matches scheduler/quota). Run only each task's named tests; the full suite goes red between tasks until M4 wiring lands (expected) — controller runs the milestone E2E.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/dlw/sources/__init__.py` (new) | package marker |
| `src/dlw/sources/base.py` (new) | `SourceDriver` Protocol + `SourceManifest`/`SourceFile`/`SourceHealth`/`SourceToken` |
| `src/dlw/sources/huggingface.py` (new) | HF driver (wraps `hf_metadata`) |
| `src/dlw/sources/hf_mirror.py` (new) | hf-mirror driver (HF-compat, no token, gated→skip) |
| `src/dlw/sources/modelscope.py` (new) | ModelScope driver (own API, no sha256) |
| `src/dlw/sources/registry.py` (new) | parse `sources.yaml`, build enabled `{id: driver}` |
| `src/dlw/sources/name_resolver.py` (new) | 3-tier name mapping from `resolver-rules.yaml` |
| `config/sources.yaml`, `config/resolver-rules.yaml` (new) | source + resolver config |
| `src/dlw/db/models/source.py` (new) | `SubtaskChunk`, `SourceSpeedSample`, `SourceBlacklist` |
| `src/dlw/services/source_speed.py` (new) | probe matrix + EWMA fusion |
| `src/dlw/services/source_combo.py` (new) | LPT assign + optimal-combo |
| `src/dlw/services/source_scheduler.py` (new) | `plan_task_sources` (resolve→probe→assign→persist) |
| `src/dlw/services/source_blacklist.py` (new) | blacklist transitions/queries |
| `src/dlw/api/source_proxy.py` (new) | `/api/v1/source-proxy/subtask/{id}` |
| `src/dlw/alembic/versions/<rev>_p3sp2_multi_source.py` (new) | cols + 3 tables |
| `src/dlw/executor/client.py` (modify) | add `stream_source` |
| `src/dlw/executor/chunk_downloader.py` (modify) | use `stream_source` |
| `src/dlw/services/scheduler.py` (modify) | sha256-authority gate on report |
| `src/dlw/services/task_service.py` (modify) | accept new TaskCreate fields |
| `src/dlw/schemas/task.py` (modify) | `source_strategy`/`source_blacklist`/`trust_non_hf_sha256` |
| `src/dlw/main.py` (modify) | lifespan registry/resolver + `_scheduling_loop` + `_rebalance_loop` |
| `src/dlw/config.py` (modify) | SP2 settings |
| `tests/conftest.py` (modify) | extend `make_app_with_state` (registry/resolver) |
| `docs/operator/multi-source.md` (new) | operator guide |

---

# Milestone M1 — Source Layer

### Task 1: Config additions + `pyyaml` dep

**Files:** Modify `src/dlw/config.py`, `pyproject.toml`; Test (extend) `tests/test_config.py`

- [ ] **Step 1: Write the failing test** — append to `tests/test_config.py`:
```python
def test_sp2_source_settings_defaults():
    from dlw.config import get_settings
    get_settings.cache_clear()
    s = get_settings()
    assert s.sources_yaml_path == "config/sources.yaml"
    assert s.resolver_rules_path == "config/resolver-rules.yaml"
    assert s.probe_size_mb == 32
    assert s.probe_timeout_s == 8.0
    assert s.chunk_level_min_file_mb == 100
    assert s.speed_ewma_alpha == 0.3
    assert s.sha_mismatch_blacklist_hours == 24
    assert s.rebalance_interval_seconds == 60.0
    get_settings.cache_clear()
```

- [ ] **Step 2: Run** `uv run pytest tests/test_config.py::test_sp2_source_settings_defaults -v` → FAIL (no attrs).

- [ ] **Step 3: Implement** — in `src/dlw/config.py`, after the Phase 3 SP1 block (after `auth_tenant_rules_json`), add:
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
Add `"pyyaml>=6,<7"` to `pyproject.toml` `[project] dependencies` (after `pydantic-settings`). Run `uv lock` then `uv sync --all-groups`. NOTE: `pyyaml` is already transitively locked (via `huggingface_hub`/`uvicorn[standard]`), so `import yaml` works today and the `uv.lock` diff will be near-empty — this step promotes it to a *direct* dependency for correctness; a tiny/empty `uv.lock` diff is expected, not an error.

- [ ] **Step 4: Run** `uv run pytest tests/test_config.py -v` → all PASS.

- [ ] **Step 5: Commit**
```bash
git add src/dlw/config.py pyproject.toml uv.lock tests/test_config.py
git commit -m "feat(sp2): multi-source config + pyyaml dep"
```

---

### Task 2: `SourceDriver` Protocol + dataclasses

**Files:** Create `src/dlw/sources/__init__.py`, `src/dlw/sources/base.py`; Test `tests/sources/__init__.py`, `tests/sources/test_base.py`

- [ ] **Step 1: Write the failing test** — create empty `tests/sources/__init__.py`, then `tests/sources/test_base.py`:
```python
"""SourceDriver Protocol + dataclasses (Phase 3 SP2)."""
from __future__ import annotations

from dlw.sources.base import (
    SourceFile,
    SourceHealth,
    SourceManifest,
    SourceToken,
)


def test_sourcefile_defaults():
    f = SourceFile(filename="model.safetensors", size=10, sha256=None,
                   download_ref="r")
    assert f.filename == "model.safetensors" and f.sha256 is None


def test_manifest_holds_files():
    m = SourceManifest(source_id="huggingface", repo_id_in_source="o/r",
                        revision_in_source="abc", files=[
                            SourceFile("a", 1, "x" * 64, "ref")],
                        has_lfs_sha256=True)
    assert m.source_id == "huggingface" and len(m.files) == 1


def test_health_and_token():
    assert SourceHealth(ok=True, latency_ms=12.0).ok is True
    t = SourceToken(scheme="bearer", value="secret")
    assert t.value == "secret" and "secret" not in repr(t)
```

- [ ] **Step 2: Run** `uv run pytest tests/sources/test_base.py -v` → FAIL (module missing).

- [ ] **Step 3: Implement** — create empty `src/dlw/sources/__init__.py`, then `src/dlw/sources/base.py`:
```python
"""SourceDriver abstraction (Phase 3 SP2; design doc 06 §1.3)."""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class SourceFile:
    filename: str            # normalized HF-style path (cross-source key)
    size: int | None
    sha256: str | None       # only HF / hf_mirror populate this
    download_ref: str        # source-specific URL or object key


@dataclass(frozen=True)
class SourceManifest:
    source_id: str
    repo_id_in_source: str
    revision_in_source: str
    files: list[SourceFile]
    has_lfs_sha256: bool


@dataclass(frozen=True)
class SourceHealth:
    ok: bool
    latency_ms: float


@dataclass(frozen=True)
class SourceToken:
    scheme: str              # "bearer" | "none"
    value: str = field(default="", repr=False)   # never in repr/logs (INV 2)


@runtime_checkable
class SourceDriver(Protocol):
    id: str
    domain: str
    provides_sha256: bool

    async def resolve(
        self, repo_id: str, revision: str
    ) -> SourceManifest | None: ...

    def download_url(self, file: SourceFile) -> str: ...

    def auth_token(self, tenant_hf_token: str | None) -> SourceToken: ...

    async def health_check(self) -> SourceHealth: ...

    def estimate_cost(self, n_bytes: int, region: str) -> Decimal: ...
```
(Design note: `download_range` from the spec is realized as `download_url(file)` + the proxy issuing the ranged GET — keeps drivers pure/sync-URL and centralizes streaming/retry in `source_proxy.py`. `auth_token` returns the controller-side cred the proxy injects; the executor never sees it.)

- [ ] **Step 4: Run** `uv run pytest tests/sources/test_base.py -v` → 3 PASS.

- [ ] **Step 5: Commit**
```bash
git add src/dlw/sources/__init__.py src/dlw/sources/base.py tests/sources/
git commit -m "feat(sp2): SourceDriver Protocol + manifest/file/token dataclasses"
```

---

### Task 3: HuggingFace + hf_mirror drivers

**Files:** Create `src/dlw/sources/huggingface.py`, `src/dlw/sources/hf_mirror.py`; Test `tests/sources/test_hf_drivers.py`

- [ ] **Step 1: Write the failing test** — `tests/sources/test_hf_drivers.py`:
```python
"""HF + hf_mirror drivers (Phase 3 SP2)."""
from __future__ import annotations

import pytest

from dlw.services.hf_metadata import RepoFile
from dlw.sources.hf_mirror import HfMirrorDriver
from dlw.sources.huggingface import HuggingFaceDriver


@pytest.fixture
def _patch_list(monkeypatch):
    async def fake(repo_id, revision, *, hf_endpoint, hf_token):
        assert revision == "abc"
        return [RepoFile(path="model.safetensors", size=64, sha256="a" * 64),
                RepoFile(path="config.json", size=4, sha256=None)]
    monkeypatch.setattr("dlw.sources.huggingface.list_repo_tree", fake)
    monkeypatch.setattr("dlw.sources.hf_mirror.list_repo_tree", fake)


async def test_hf_resolve(_patch_list):
    d = HuggingFaceDriver(base_url="https://huggingface.co", hf_token="tok")
    m = await d.resolve("o/r", "abc")
    assert m is not None
    assert m.source_id == "huggingface" and m.has_lfs_sha256 is True
    assert {f.filename for f in m.files} == {"model.safetensors", "config.json"}
    assert d.provides_sha256 is True
    assert d.download_url(m.files[0]).endswith(
        "/o/r/resolve/abc/model.safetensors")
    assert d.auth_token("tok").value == "tok"


async def test_hf_mirror_no_token_and_base(_patch_list):
    d = HfMirrorDriver(base_url="https://hf-mirror.com")
    m = await d.resolve("o/r", "abc")
    assert m.source_id == "hf_mirror"
    assert d.download_url(m.files[0]).startswith("https://hf-mirror.com/")
    assert d.auth_token("tok").scheme == "none"


async def test_hf_mirror_gated_returns_none(monkeypatch):
    from dlw.services.hf_metadata import HfPrivateOrAuthRequired

    async def gated(*a, **k):
        raise HfPrivateOrAuthRequired("gated")
    monkeypatch.setattr("dlw.sources.hf_mirror.list_repo_tree", gated)
    d = HfMirrorDriver(base_url="https://hf-mirror.com")
    assert await d.resolve("o/gated", "abc") is None
```

- [ ] **Step 2: Run** `uv run pytest tests/sources/test_hf_drivers.py -v` → FAIL.

- [ ] **Step 3: Implement** — `src/dlw/sources/huggingface.py`:
```python
"""HuggingFace SourceDriver — wraps the existing hf_metadata path (SP2)."""
from __future__ import annotations

from decimal import Decimal

from dlw.services.hf_metadata import (
    HfNetworkError,
    HfPrivateOrAuthRequired,
    RepoNotFound,
    list_repo_tree,
)
from dlw.sources.base import (
    SourceFile,
    SourceHealth,
    SourceManifest,
    SourceToken,
)


class HuggingFaceDriver:
    id = "huggingface"
    domain = "huggingface.co"
    provides_sha256 = True

    def __init__(self, *, base_url: str, hf_token: str | None) -> None:
        self._base = base_url.rstrip("/")
        self._token = hf_token

    async def resolve(
        self, repo_id: str, revision: str
    ) -> SourceManifest | None:
        try:
            files = await list_repo_tree(
                repo_id, revision,
                hf_endpoint=self._base, hf_token=self._token)
        except (RepoNotFound,):
            return None
        except (HfPrivateOrAuthRequired, HfNetworkError):
            raise
        sf = [SourceFile(filename=f.path, size=f.size, sha256=f.sha256,
                         download_ref=f"{repo_id}/resolve/{revision}/{f.path}")
              for f in files]
        return SourceManifest(
            source_id=self.id, repo_id_in_source=repo_id,
            revision_in_source=revision, files=sf,
            has_lfs_sha256=any(f.sha256 for f in sf))

    def download_url(self, file: SourceFile) -> str:
        return f"{self._base}/{file.download_ref}"

    def auth_token(self, tenant_hf_token: str | None) -> SourceToken:
        tok = tenant_hf_token or self._token
        return (SourceToken(scheme="bearer", value=tok) if tok
                else SourceToken(scheme="none"))

    async def health_check(self) -> SourceHealth:
        return SourceHealth(ok=True, latency_ms=0.0)

    def estimate_cost(self, n_bytes: int, region: str) -> Decimal:
        return Decimal("0.09") * Decimal(n_bytes) / Decimal(1_000_000_000)
```
`src/dlw/sources/hf_mirror.py`:
```python
"""hf-mirror.com SourceDriver — HF-compatible, no token, gated→skip (SP2)."""
from __future__ import annotations

from decimal import Decimal

from dlw.services.hf_metadata import (
    HfNetworkError,
    HfPrivateOrAuthRequired,
    RepoNotFound,
    list_repo_tree,
)
from dlw.sources.base import (
    SourceFile,
    SourceHealth,
    SourceManifest,
    SourceToken,
)


class HfMirrorDriver:
    id = "hf_mirror"
    domain = "hf-mirror.com"
    provides_sha256 = True

    def __init__(self, *, base_url: str) -> None:
        self._base = base_url.rstrip("/")

    async def resolve(
        self, repo_id: str, revision: str
    ) -> SourceManifest | None:
        try:
            files = await list_repo_tree(
                repo_id, revision, hf_endpoint=self._base, hf_token=None)
        except RepoNotFound:
            return None
        except HfPrivateOrAuthRequired:
            return None    # gated: public mirror can't serve it — skip
        except HfNetworkError:
            raise
        sf = [SourceFile(filename=f.path, size=f.size, sha256=f.sha256,
                         download_ref=f"{repo_id}/resolve/{revision}/{f.path}")
              for f in files]
        return SourceManifest(
            source_id=self.id, repo_id_in_source=repo_id,
            revision_in_source=revision, files=sf,
            has_lfs_sha256=any(f.sha256 for f in sf))

    def download_url(self, file: SourceFile) -> str:
        return f"{self._base}/{file.download_ref}"

    def auth_token(self, tenant_hf_token: str | None) -> SourceToken:
        return SourceToken(scheme="none")

    async def health_check(self) -> SourceHealth:
        return SourceHealth(ok=True, latency_ms=0.0)

    def estimate_cost(self, n_bytes: int, region: str) -> Decimal:
        return Decimal(0)
```

- [ ] **Step 4: Run** `uv run pytest tests/sources/test_hf_drivers.py -v` → 3 PASS.

- [ ] **Step 5: Commit**
```bash
git add src/dlw/sources/huggingface.py src/dlw/sources/hf_mirror.py tests/sources/test_hf_drivers.py
git commit -m "feat(sp2): HuggingFace + hf_mirror SourceDrivers"
```

---

### Task 4: ModelScope driver

**Files:** Create `src/dlw/sources/modelscope.py`; Test `tests/sources/test_modelscope_driver.py`

- [ ] **Step 1: Write the failing test** — `tests/sources/test_modelscope_driver.py`:
```python
"""ModelScope driver (Phase 3 SP2)."""
from __future__ import annotations

import httpx
import pytest

from dlw.sources.modelscope import ModelScopeDriver


def _handler(request: httpx.Request) -> httpx.Response:
    assert "modelscope.cn" in str(request.url)
    if "/repo?Revision=" in str(request.url) and "FilePath" not in str(request.url):
        return httpx.Response(200, json={"Data": {"Files": [
            {"Path": "model.safetensors", "Size": 64},
            {"Path": "config.json", "Size": 4}]}})
    return httpx.Response(404)


@pytest.fixture
def _drv():
    return ModelScopeDriver(
        base_url="https://www.modelscope.cn",
        transport=httpx.MockTransport(_handler))


async def test_modelscope_resolve_no_sha(_drv):
    m = await _drv.resolve("qwen/Qwen3-7B", "v1")
    assert m is not None
    assert m.source_id == "modelscope" and m.has_lfs_sha256 is False
    assert all(f.sha256 is None for f in m.files)
    assert {f.filename for f in m.files} == {"model.safetensors", "config.json"}
    assert _drv.provides_sha256 is False


async def test_modelscope_download_url(_drv):
    m = await _drv.resolve("qwen/Qwen3-7B", "v1")
    url = _drv.download_url(m.files[0])
    assert "FilePath=model.safetensors" in url and "Revision=v1" in url


async def test_modelscope_missing_repo_returns_none():
    d = ModelScopeDriver(
        base_url="https://www.modelscope.cn",
        transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    assert await d.resolve("no/such", "v1") is None
```

- [ ] **Step 2: Run** `uv run pytest tests/sources/test_modelscope_driver.py -v` → FAIL.

- [ ] **Step 3: Implement** — `src/dlw/sources/modelscope.py`:
```python
"""ModelScope SourceDriver — raw httpx, no official sha256 (SP2; doc §1.9.3)."""
from __future__ import annotations

from decimal import Decimal
from urllib.parse import quote

import httpx

from dlw.sources.base import (
    SourceFile,
    SourceHealth,
    SourceManifest,
    SourceToken,
)


class ModelScopeDriver:
    id = "modelscope"
    domain = "modelscope.cn"
    provides_sha256 = False

    def __init__(self, *, base_url: str,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=30, transport=self._transport)

    async def resolve(
        self, repo_id: str, revision: str
    ) -> SourceManifest | None:
        url = f"{self._base}/api/v1/models/{repo_id}/repo?Revision={revision}"
        async with self._client() as c:
            r = await c.get(url)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json().get("Data", {}).get("Files", [])
        sf = [SourceFile(filename=d["Path"], size=d.get("Size"),
                         sha256=None,
                         download_ref=f"{repo_id}|{revision}|{d['Path']}")
              for d in data]
        return SourceManifest(
            source_id=self.id, repo_id_in_source=repo_id,
            revision_in_source=revision, files=sf, has_lfs_sha256=False)

    def download_url(self, file: SourceFile) -> str:
        repo, rev, path = file.download_ref.split("|", 2)
        return (f"{self._base}/api/v1/models/{repo}/repo"
                f"?Revision={rev}&FilePath={quote(path)}")

    def auth_token(self, tenant_hf_token: str | None) -> SourceToken:
        return SourceToken(scheme="none")

    async def health_check(self) -> SourceHealth:
        return SourceHealth(ok=True, latency_ms=0.0)

    def estimate_cost(self, n_bytes: int, region: str) -> Decimal:
        return Decimal(0)
```

- [ ] **Step 4: Run** `uv run pytest tests/sources/test_modelscope_driver.py -v` → 3 PASS.

- [ ] **Step 5: Commit**
```bash
git add src/dlw/sources/modelscope.py tests/sources/test_modelscope_driver.py
git commit -m "feat(sp2): ModelScope SourceDriver"
```

---

### Task 5: Registry + `sources.yaml`

**Files:** Create `src/dlw/sources/registry.py`, `config/sources.yaml`; Test `tests/sources/test_registry.py`

- [ ] **Step 1: Write the failing test** — `tests/sources/test_registry.py`:
```python
"""Source registry from sources.yaml (Phase 3 SP2)."""
from __future__ import annotations

from dlw.sources.registry import load_registry

_YAML = """
sources:
  - id: huggingface
    enabled: true
    driver: huggingface
    config: {base_url: https://huggingface.co}
  - id: hf_mirror
    enabled: true
    driver: hf_mirror
    config: {base_url: https://hf-mirror.com}
  - id: modelscope
    enabled: false
    driver: modelscope
    config: {base_url: https://www.modelscope.cn}
  - id: corp
    enabled: true
    driver: s3_mirror
    config: {}
regional_defaults:
  cn-north: [hf_mirror, modelscope, huggingface]
"""


def test_only_enabled_supported(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text(_YAML, encoding="utf-8")
    reg = load_registry(str(p), hf_token="tk")
    assert set(reg.enabled_ids()) == {"huggingface", "hf_mirror"}  # ms off, s3 unsupported
    assert reg.get("huggingface").id == "huggingface"
    assert reg.get("missing") is None
    assert reg.regional_defaults["cn-north"][0] == "hf_mirror"


def test_modelscope_enabled(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text(_YAML.replace("id: modelscope\n    enabled: false",
                               "id: modelscope\n    enabled: true"),
                 encoding="utf-8")
    reg = load_registry(str(p), hf_token=None)
    assert "modelscope" in reg.enabled_ids()
```

- [ ] **Step 2: Run** `uv run pytest tests/sources/test_registry.py -v` → FAIL.

- [ ] **Step 3: Implement** — `src/dlw/sources/registry.py`:
```python
"""sources.yaml → enabled SourceDriver registry (Phase 3 SP2)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml

from dlw.sources.base import SourceDriver
from dlw.sources.hf_mirror import HfMirrorDriver
from dlw.sources.huggingface import HuggingFaceDriver
from dlw.sources.modelscope import ModelScopeDriver

_SUPPORTED = {"huggingface", "hf_mirror", "modelscope"}


@dataclass
class SourceRegistry:
    _drivers: dict[str, SourceDriver]
    regional_defaults: dict[str, list[str]] = field(default_factory=dict)

    def enabled_ids(self) -> list[str]:
        return list(self._drivers.keys())

    def get(self, source_id: str) -> SourceDriver | None:
        return self._drivers.get(source_id)


def _build(driver: str, cfg: dict[str, Any],
           hf_token: str | None) -> SourceDriver | None:
    if driver == "huggingface":
        return HuggingFaceDriver(
            base_url=cfg.get("base_url", "https://huggingface.co"),
            hf_token=hf_token)
    if driver == "hf_mirror":
        return HfMirrorDriver(
            base_url=cfg.get("base_url", "https://hf-mirror.com"))
    if driver == "modelscope":
        return ModelScopeDriver(
            base_url=cfg.get("base_url", "https://www.modelscope.cn"))
    return None   # unsupported driver (s3_mirror/wisemodel/...) — skip


def load_registry(path: str, *, hf_token: str | None) -> SourceRegistry:
    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    drivers: dict[str, SourceDriver] = {}
    for entry in doc.get("sources", []):
        if not entry.get("enabled"):
            continue
        if entry.get("driver") not in _SUPPORTED:
            continue
        d = _build(entry["driver"], entry.get("config") or {}, hf_token)
        if d is not None:
            drivers[entry["id"]] = d
    return SourceRegistry(_drivers=drivers,
                          regional_defaults=doc.get("regional_defaults", {}))
```
Create `config/sources.yaml`:
```yaml
sources:
  - id: huggingface
    enabled: true
    driver: huggingface
    config: {base_url: "https://huggingface.co", timeout_seconds: 30}
    cost_per_gb_egress: 0.09
  - id: hf_mirror
    enabled: true
    driver: hf_mirror
    config: {base_url: "https://hf-mirror.com", timeout_seconds: 30}
    cost_per_gb_egress: 0.0
  - id: modelscope
    enabled: true
    driver: modelscope
    config: {base_url: "https://www.modelscope.cn", timeout_seconds: 30}
    cost_per_gb_egress: 0.0
balancing:
  speed_ewma_alpha: 0.3
  chunk_level_min_file_mb: 100
regional_defaults:
  cn-north: ["hf_mirror", "modelscope", "huggingface"]
  us-east: ["huggingface"]
```

- [ ] **Step 4: Run** `uv run pytest tests/sources/test_registry.py -v` → 2 PASS.

- [ ] **Step 5: Commit**
```bash
git add src/dlw/sources/registry.py config/sources.yaml tests/sources/test_registry.py
git commit -m "feat(sp2): source registry + sources.yaml"
```

---

### Task 6: NameResolver + `resolver-rules.yaml`

**Files:** Create `src/dlw/sources/name_resolver.py`, `config/resolver-rules.yaml`; Test `tests/sources/test_name_resolver.py`

- [ ] **Step 1: Write the failing test** — `tests/sources/test_name_resolver.py`:
```python
"""NameResolver 3-tier (Phase 3 SP2; doc §1.5)."""
from __future__ import annotations

from dlw.sources.name_resolver import NameResolver

_RULES = """
identity_organizations: [deepseek-ai, Qwen, THUDM]
aliases:
  - hf_org: meta-llama
    modelscope_org: LLM-Research
    transform: "Meta-{name}"
per_model_overrides:
  - hf: "weird-org/weird-model"
    modelscope: "diff-org/diff-name"
"""


def _r(tmp_path):
    p = tmp_path / "rr.yaml"
    p.write_text(_RULES, encoding="utf-8")
    return NameResolver.from_file(str(p))


def test_huggingface_is_always_identity(tmp_path):
    r = _r(tmp_path)
    assert r.resolve("huggingface", "any-org/any-model") == "any-org/any-model"


def test_identity_org(tmp_path):
    r = _r(tmp_path)
    assert r.resolve("modelscope", "deepseek-ai/DeepSeek-V3") == "deepseek-ai/DeepSeek-V3"


def test_alias_transform(tmp_path):
    r = _r(tmp_path)
    assert r.resolve("modelscope", "meta-llama/Llama-3.1-8B") == "LLM-Research/Meta-Llama-3.1-8B"


def test_per_model_override(tmp_path):
    r = _r(tmp_path)
    assert r.resolve("modelscope", "weird-org/weird-model") == "diff-org/diff-name"


def test_unknown_returns_none(tmp_path):
    r = _r(tmp_path)
    assert r.resolve("modelscope", "rando-org/rando-model") is None
```

- [ ] **Step 2: Run** `uv run pytest tests/sources/test_name_resolver.py -v` → FAIL.

- [ ] **Step 3: Implement** — `src/dlw/sources/name_resolver.py`:
```python
"""3-tier source name resolution (Phase 3 SP2; doc §1.5).

Tier 1 identity (HF, or org in identity_organizations); tier 2 alias /
per-model rules from resolver-rules.yaml; tier 3 source search-API (deferred
to a stub that returns None — wiring point for v2.1; cache scaffold present)."""
from __future__ import annotations

from dataclasses import dataclass

import yaml


@dataclass
class _Alias:
    hf_org: str
    ms_org: str
    transform: str   # e.g. "Meta-{name}"


class NameResolver:
    def __init__(self, *, identity_orgs: set[str], aliases: list[_Alias],
                 overrides: dict[str, str]) -> None:
        self._identity = identity_orgs
        self._aliases = {a.hf_org: a for a in aliases}
        self._overrides = overrides           # "hf_repo" -> "src_repo"
        self._search_cache: dict[tuple[str, str], str] = {}

    @classmethod
    def from_file(cls, path: str) -> NameResolver:
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
        aliases = [_Alias(a["hf_org"], a["modelscope_org"], a["transform"])
                   for a in doc.get("aliases", [])]
        overrides = {o["hf"]: o["modelscope"]
                     for o in doc.get("per_model_overrides", [])}
        return cls(identity_orgs=set(doc.get("identity_organizations", [])),
                   aliases=aliases, overrides=overrides)

    def resolve(self, source_id: str, hf_repo_id: str) -> str | None:
        if source_id == "huggingface" or source_id == "hf_mirror":
            return hf_repo_id
        if hf_repo_id in self._overrides:
            return self._overrides[hf_repo_id]
        org, _, name = hf_repo_id.partition("/")
        if org in self._identity:
            return hf_repo_id
        a = self._aliases.get(org)
        if a is not None:
            return f"{a.ms_org}/{a.transform.format(name=name)}"
        return self._search_cache.get((source_id, hf_repo_id))   # tier 3 stub
```
Create `config/resolver-rules.yaml`:
```yaml
identity_organizations:
  - deepseek-ai
  - Qwen
  - 01-ai
  - THUDM
  - baichuan-inc
  - mistralai
aliases:
  - hf_org: meta-llama
    modelscope_org: LLM-Research
    transform: "Meta-{name}"
per_model_overrides: []
```

- [ ] **Step 4: Run** `uv run pytest tests/sources/test_name_resolver.py -v` → 5 PASS.

- [ ] **Step 5: Commit**
```bash
git add src/dlw/sources/name_resolver.py config/resolver-rules.yaml tests/sources/test_name_resolver.py
git commit -m "feat(sp2): NameResolver 3-tier + resolver-rules.yaml"
```

---

# Milestone M2 — Schema + Models

### Task 7: Models + migration

**Files:** Create `src/dlw/db/models/source.py`, migration; Modify `src/dlw/db/models/__init__.py`, `src/dlw/schemas/task.py`; Test `tests/db/test_p3sp2_migration.py`

- [ ] **Step 1: Write the failing test** — `tests/db/test_p3sp2_migration.py`:
```python
"""SP2 migration: 3 tables + task/subtask source columns."""
from __future__ import annotations

import pytest
from sqlalchemy import text

import dlw.db.models  # noqa: F401

pytestmark = pytest.mark.slow


async def test_tables_and_columns(engine):
    from dlw.db.base import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        names = {r[0] for r in await conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public'"))}
        assert {"subtask_chunks", "source_speed_samples",
                "source_blacklist"} <= names
        cols = {r[0] for r in await conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='download_tasks'"))}
        assert {"source_strategy", "source_blacklist",
                "trust_non_hf_sha256"} <= cols
        scols = {r[0] for r in await conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='file_subtasks'"))}
        assert {"source_id", "is_chunked"} <= scols
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
```

- [ ] **Step 2: Run** `uv run pytest tests/db/test_p3sp2_migration.py -v` → FAIL.

- [ ] **Step 3: Implement** — `src/dlw/db/models/source.py`:
```python
"""Multi-source models (Phase 3 SP2; doc 06 §1.4/§1.7)."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from dlw.db.base import Base


class SubtaskChunk(Base):
    __tablename__ = "subtask_chunks"
    __table_args__ = (UniqueConstraint("subtask_id", "chunk_index"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    subtask_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("file_subtasks.id", ondelete="CASCADE"), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    byte_start: Mapped[int] = mapped_column(BigInteger, nullable=False)
    byte_end: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_id: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    sha256_partial: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bytes_done: Mapped[int] = mapped_column(BigInteger, default=0,
                                            nullable=False)


class SourceSpeedSample(Base):
    __tablename__ = "source_speed_samples"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    executor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(32), nullable=False)
    measured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    bytes_per_sec: Mapped[float] = mapped_column(Float, nullable=False)
    sample_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    is_active_probe: Mapped[bool] = mapped_column(default=False,
                                                  nullable=False)


class SourceBlacklist(Base):
    __tablename__ = "source_blacklist"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[str] = mapped_column(String(32), nullable=False)
    repo_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
```
In `src/dlw/db/models/__init__.py` add `from dlw.db.models.source import SourceBlacklist, SourceSpeedSample, SubtaskChunk` and add `"SourceBlacklist", "SourceSpeedSample", "SubtaskChunk"` to `__all__` (keep sorted). In `src/dlw/schemas/task.py` `TaskCreate` add fields:
```python
    source_strategy: str = Field(default="auto_balance", max_length=32)
    source_blacklist: list[str] = Field(default_factory=list)
    trust_non_hf_sha256: bool = Field(default=False)
```
Generate migration: `uv run alembic revision -m "p3sp2 multi source"`. Set `down_revision = "a4bed702cdb3"`, imports `import sqlalchemy as sa`, `from alembic import op`, `from sqlalchemy.dialects import postgresql`. Body:
```python
def upgrade() -> None:
    op.add_column("download_tasks", sa.Column(
        "source_strategy", sa.String(32), nullable=False,
        server_default="auto_balance"))
    op.add_column("download_tasks", sa.Column(
        "source_blacklist", postgresql.JSONB(), nullable=False,
        server_default="[]"))
    op.add_column("download_tasks", sa.Column(
        "trust_non_hf_sha256", sa.Boolean(), nullable=False,
        server_default=sa.false()))
    op.add_column("file_subtasks", sa.Column(
        "source_id", sa.String(32), nullable=True))
    op.add_column("file_subtasks", sa.Column(
        "is_chunked", sa.Boolean(), nullable=False,
        server_default=sa.false()))
    op.create_table(
        "subtask_chunks",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("subtask_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("file_subtasks.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("byte_start", sa.BigInteger(), nullable=False),
        sa.Column("byte_end", sa.BigInteger(), nullable=False),
        sa.Column("source_id", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("sha256_partial", sa.String(64), nullable=True),
        sa.Column("bytes_done", sa.BigInteger(), nullable=False,
                  server_default="0"),
        sa.UniqueConstraint("subtask_id", "chunk_index"),
    )
    op.create_index("idx_chunk_sub_status", "subtask_chunks",
                    ["subtask_id", "status"])
    op.create_table(
        "source_speed_samples",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("executor_id", sa.String(64), nullable=False),
        sa.Column("source_id", sa.String(32), nullable=False),
        sa.Column("measured_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("bytes_per_sec", sa.Float(), nullable=False),
        sa.Column("sample_size", sa.BigInteger(), nullable=False),
        sa.Column("is_active_probe", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
    )
    op.create_index("idx_speed_recent", "source_speed_samples",
                    ["executor_id", "source_id", "measured_at"])
    op.create_table(
        "source_blacklist",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("source_id", sa.String(32), nullable=False),
        sa.Column("repo_id", sa.String(256), nullable=True),
        sa.Column("filename", sa.String(512), nullable=True),
        sa.Column("until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_blacklist_lookup", "source_blacklist",
                    ["source_id", "repo_id", "until"])


def downgrade() -> None:
    op.drop_index("idx_blacklist_lookup", "source_blacklist")
    op.drop_table("source_blacklist")
    op.drop_index("idx_speed_recent", "source_speed_samples")
    op.drop_table("source_speed_samples")
    op.drop_index("idx_chunk_sub_status", "subtask_chunks")
    op.drop_table("subtask_chunks")
    op.drop_column("file_subtasks", "is_chunked")
    op.drop_column("file_subtasks", "source_id")
    op.drop_column("download_tasks", "trust_non_hf_sha256")
    op.drop_column("download_tasks", "source_blacklist")
    op.drop_column("download_tasks", "source_strategy")
```
Also add the matching SQLAlchemy columns to the existing models so `Base.metadata.create_all` (used by tests) builds them: in `src/dlw/db/models/task.py` `DownloadTask` add `source_strategy: Mapped[str] = mapped_column(String(32), default="auto_balance", nullable=False)`, `source_blacklist: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)` (import `from sqlalchemy.dialects.postgresql import JSONB`), `trust_non_hf_sha256: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)`; in `FileSubTask` add `source_id: Mapped[str | None] = mapped_column(String(32), nullable=True)`, `is_chunked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)`. (Use the imports already at the top of `task.py`; add `JSONB` if absent.)

- [ ] **Step 4: Run** `uv run pytest tests/db/test_p3sp2_migration.py -v` (PASS), then `uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head` (clean).

- [ ] **Step 5: Commit**
```bash
git add src/dlw/db/models/source.py src/dlw/db/models/__init__.py src/dlw/db/models/task.py src/dlw/schemas/task.py src/dlw/alembic/versions/ tests/db/test_p3sp2_migration.py
git commit -m "feat(sp2): SubtaskChunk/SourceSpeedSample/SourceBlacklist models + migration"
```

---

# Milestone M3 — Planner

### Task 8: Speed service (EWMA fusion)

**Files:** Create `src/dlw/services/source_speed.py`; Test `tests/services/test_source_speed.py`

Controller-side probe (per spec banner ruling 6d): the controller itself does one small ranged GET per source via the driver's `download_url`, times it, returns bytes/sec. Per-executor probe-through-proxy is deferred to v2.1.

- [ ] **Step 1: Write the failing test** — `tests/services/test_source_speed.py`:
```python
"""Speed EWMA fusion + controller-side probe (Phase 3 SP2; doc §1.7/§1.8)."""
from __future__ import annotations

import httpx
import pytest

from dlw.services.source_speed import (
    fuse_ewma,
    pick_probe_size_bytes,
    probe_source_speed,
)
from dlw.sources.base import SourceFile


def test_fuse_no_history_uses_live():
    assert fuse_ewma(live=1000.0, hist=None, hist_weight=0.3) == 1000.0


def test_fuse_blends():
    assert fuse_ewma(live=1000.0, hist=500.0, hist_weight=0.3) == 850.0


def test_probe_size():
    assert pick_probe_size_bytes(probe_size_mb=32) == 32 * 1024 * 1024


class _Drv:
    def download_url(self, f):
        return "https://src/x"

    def auth_token(self, t):
        from dlw.sources.base import SourceToken
        return SourceToken(scheme="none")


async def test_probe_returns_positive_speed():
    transport = httpx.MockTransport(
        lambda r: httpx.Response(206, content=b"x" * 4096))
    bps = await probe_source_speed(
        _Drv(), SourceFile("m", 4096, None, "ref"),
        probe_bytes=4096, timeout_s=5.0, hf_token=None, transport=transport)
    assert bps > 0.0


async def test_probe_failure_returns_zero():
    def boom(r):
        raise httpx.ConnectError("down")
    bps = await probe_source_speed(
        _Drv(), SourceFile("m", 4096, None, "ref"),
        probe_bytes=4096, timeout_s=5.0, hf_token=None,
        transport=httpx.MockTransport(boom))
    assert bps == 0.0
```

- [ ] **Step 2: Run** `uv run pytest tests/services/test_source_speed.py -v` → FAIL.

- [ ] **Step 3: Implement** — `src/dlw/services/source_speed.py`:
```python
"""Source speed: controller-side probe + EWMA fusion (Phase 3 SP2)."""
from __future__ import annotations

import time
from typing import Any

import httpx


def fuse_ewma(*, live: float, hist: float | None,
              hist_weight: float) -> float:
    if hist is None:
        return live
    return (1.0 - hist_weight) * live + hist_weight * hist


def pick_probe_size_bytes(*, probe_size_mb: int) -> int:
    return probe_size_mb * 1024 * 1024


async def probe_source_speed(
    driver: Any, file: Any, *, probe_bytes: int, timeout_s: float,
    hf_token: str | None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> float:
    """One ranged GET (controller→source) timing bytes/sec. 0.0 on any
    failure (effect: that source is treated as unavailable for this task)."""
    url = driver.download_url(file)
    tok = driver.auth_token(hf_token)
    headers = {"Range": f"bytes=0-{max(0, probe_bytes - 1)}"}
    if tok.scheme == "bearer" and tok.value:
        headers["Authorization"] = f"Bearer {tok.value}"
    try:
        async with httpx.AsyncClient(timeout=timeout_s, transport=transport,
                                     follow_redirects=True) as c:
            start = time.monotonic()
            recv = 0
            async with c.stream("GET", url, headers=headers) as resp:
                if resp.status_code >= 400:
                    return 0.0
                async for buf in resp.aiter_bytes(64 * 1024):
                    recv += len(buf)
            elapsed = time.monotonic() - start
        return recv / elapsed if elapsed > 0 and recv > 0 else 0.0
    except Exception:
        return 0.0
```

- [ ] **Step 4: Run** `uv run pytest tests/services/test_source_speed.py -v` → 5 PASS.

- [ ] **Step 5: Commit**
```bash
git add src/dlw/services/source_speed.py tests/services/test_source_speed.py
git commit -m "feat(sp2): source speed EWMA fusion"
```

---

### Task 9: LPT assignment + optimal-combo

**Files:** Create `src/dlw/services/source_combo.py`; Test `tests/services/test_source_combo.py`

- [ ] **Step 1: Write the failing test** — `tests/services/test_source_combo.py`:
```python
"""LPT greedy + optimal-combo (Phase 3 SP2; doc §1.6/§1.8, OR-V21-04)."""
from __future__ import annotations

from dlw.services.source_combo import assign_files_lpt, solve_optimal_combo


def test_lpt_balances_by_completion_time():
    files = {"a": 100, "b": 100, "c": 50}
    speeds = {"s1": 10.0, "s2": 5.0}
    assign = assign_files_lpt(files, speeds)
    assert set(assign.values()) <= {"s1", "s2"}
    # largest files go to the faster source first
    assert assign["a"] == "s1"


def test_lpt_single_source_degenerate():
    assign = assign_files_lpt({"a": 1, "b": 2}, {"only": 7.0})
    assert assign == {"a": "only", "b": "only"}


def test_combo_excludes_slow_source_by_overhead():
    # one fast source + one trivially-slow source: combo should drop the slow
    files = {"f": 1_000_000_000}
    speeds = {"fast": 1_000_000_000.0, "slow": 1.0}
    combo = solve_optimal_combo(speeds, files, overhead_pct=2.0)
    assert combo == ["fast"]


def test_combo_uses_both_when_comparable():
    files = {"a": 100, "b": 100}
    speeds = {"s1": 10.0, "s2": 10.0}
    combo = solve_optimal_combo(speeds, files, overhead_pct=2.0)
    assert set(combo) == {"s1", "s2"}
```

- [ ] **Step 2: Run** `uv run pytest tests/services/test_source_combo.py -v` → FAIL.

- [ ] **Step 3: Implement** — `src/dlw/services/source_combo.py`:
```python
"""File→source assignment: size-descending greedy heuristic (NOT bounded-
optimal LPT — doc OR-V21-04) + fastest-K combo with overhead penalty."""
from __future__ import annotations


def assign_files_lpt(
    files: dict[str, int], source_speeds: dict[str, float]
) -> dict[str, str]:
    """files: {filename: size}; source_speeds: {source_id: bytes/sec}.
    Returns {filename: source_id}. Largest-first; each file to the source
    with the earliest projected completion (load+size)/speed."""
    load = {sid: 0.0 for sid in source_speeds}
    out: dict[str, str] = {}
    for fn in sorted(files, key=lambda k: -files[k]):
        size = files[fn]
        best = min(source_speeds,
                   key=lambda sid: (load[sid] + size) / source_speeds[sid])
        out[fn] = best
        load[best] += size
    return out


def _eta(files: dict[str, int], speeds: dict[str, float]) -> float:
    assign = assign_files_lpt(files, speeds)
    load = {sid: 0.0 for sid in speeds}
    for fn, sid in assign.items():
        load[sid] += files[fn]
    return max((load[sid] / speeds[sid] for sid in speeds), default=0.0)


def solve_optimal_combo(
    source_speeds: dict[str, float], files: dict[str, int],
    *, overhead_pct: float
) -> list[str]:
    ranked = sorted(source_speeds, key=lambda s: -source_speeds[s])
    best_eta = float("inf")
    best: list[str] = ranked[:1]
    for k in range(1, len(ranked) + 1):
        combo = ranked[:k]
        sub = {s: source_speeds[s] for s in combo}
        eta = _eta(files, sub) * (1 + 0.01 * overhead_pct * (k - 1))
        if eta < best_eta:
            best_eta, best = eta, combo
        elif k > 1 and eta > best_eta * 1.05:
            break
    return best
```

- [ ] **Step 4: Run** `uv run pytest tests/services/test_source_combo.py -v` → 4 PASS.

- [ ] **Step 5: Commit**
```bash
git add src/dlw/services/source_combo.py tests/services/test_source_combo.py
git commit -m "feat(sp2): LPT greedy assignment + optimal-combo selection"
```

---

### Task 10: Blacklist service

**Files:** Create `src/dlw/services/source_blacklist.py`; Test `tests/services/test_source_blacklist.py`

- [ ] **Step 1: Write the failing test** — `tests/services/test_source_blacklist.py`:
```python
"""Source blacklist transitions (Phase 3 SP2; doc §1.7)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import dlw.db.models  # noqa: F401
from dlw.db.base import Base
from dlw.db.models.source import SourceBlacklist
from dlw.services.source_blacklist import (
    blacklist_file,
    is_blacklisted,
)

pytestmark = pytest.mark.slow


@pytest.fixture
async def factory(engine):
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)
        await c.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)


async def test_blacklist_and_check(factory):
    async with factory() as s:
        await blacklist_file(s, source_id="modelscope", repo_id="o/r",
                             filename="m.safetensors", hours=24,
                             reason="sha_mismatch")
        await s.commit()
        assert await is_blacklisted(s, "modelscope", "o/r",
                                    "m.safetensors") is True
        assert await is_blacklisted(s, "modelscope", "o/r",
                                    "other.bin") is False


async def test_expired_not_blacklisted(factory):
    async with factory() as s:
        s.add(SourceBlacklist(source_id="modelscope", repo_id="o/r",
                              filename="m", reason="x",
                              until=datetime.now(UTC) - timedelta(hours=1)))
        await s.commit()
        assert await is_blacklisted(s, "modelscope", "o/r", "m") is False
```

- [ ] **Step 2: Run** `uv run pytest tests/services/test_source_blacklist.py -v` → FAIL.

- [ ] **Step 3: Implement** — `src/dlw/services/source_blacklist.py`:
```python
"""Source/(source,repo,file) blacklist (Phase 3 SP2; doc §1.7).
Caller commits (service-layer convention)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.source import SourceBlacklist


async def blacklist_file(
    session: AsyncSession, *, source_id: str, repo_id: str,
    filename: str, hours: int, reason: str,
) -> None:
    session.add(SourceBlacklist(
        source_id=source_id, repo_id=repo_id, filename=filename,
        until=datetime.now(UTC) + timedelta(hours=hours), reason=reason))


async def is_blacklisted(
    session: AsyncSession, source_id: str, repo_id: str, filename: str
) -> bool:
    row = await session.scalar(
        select(SourceBlacklist.id).where(
            SourceBlacklist.source_id == source_id,
            SourceBlacklist.repo_id == repo_id,
            SourceBlacklist.filename == filename,
            SourceBlacklist.until > datetime.now(UTC)).limit(1))
    return row is not None
```

- [ ] **Step 4: Run** `uv run pytest tests/services/test_source_blacklist.py -v` → 2 PASS.

- [ ] **Step 5: Commit**
```bash
git add src/dlw/services/source_blacklist.py tests/services/test_source_blacklist.py
git commit -m "feat(sp2): source blacklist service"
```

---

### Task 11: Planner `plan_task_sources`

**Files:** Create `src/dlw/services/source_scheduler.py`; Test `tests/services/test_source_scheduler.py`

- [ ] **Step 1: Write the failing test** — `tests/services/test_source_scheduler.py`:
```python
"""plan_task_sources: resolve→assign→persist + HF-authority gate (SP2)."""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import dlw.db.models  # noqa: F401
from dlw.db.base import Base
from dlw.db.models.source import SubtaskChunk
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.services.source_scheduler import plan_task_sources
from dlw.sources.base import SourceFile, SourceManifest

pytestmark = pytest.mark.slow


class _FakeDriver:
    def __init__(self, sid, files, sha):
        self.id = sid
        self.provides_sha256 = sha
        self._files = files

    async def resolve(self, repo_id, revision):
        return SourceManifest(self.id, repo_id, revision, self._files,
                              has_lfs_sha256=any(
                                  f.sha256 for f in self._files))


class _FakeReg:
    def __init__(self, drivers):
        self._d = drivers

    def enabled_ids(self):
        return list(self._d)

    def get(self, sid):
        return self._d.get(sid)


class _IdResolver:
    def resolve(self, source_id, hf_repo_id):
        return hf_repo_id


@pytest.fixture
async def factory(engine):
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)
        await c.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Project, Tenant, User
    async with f() as s:
        s.add(Tenant(id=1, slug="t", display_name="T"))
        await s.flush()
        s.add_all([Project(id=1, tenant_id=1, name="d"),
                   User(id=1, tenant_id=1, oidc_subject="u", email="e",
                        role="tenant_operator"),
                   StorageBackend(id=1, tenant_id=1, name="s",
                                  backend_type="s3", config_encrypted=b"")])
        await s.commit()
    yield f
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)


def _files():
    return [SourceFile("model.safetensors", 200 * 1024 * 1024, "a" * 64,
                       "ref"),
            SourceFile("config.json", 10, None, "ref2")]


async def test_plan_assigns_and_persists(factory):
    async with factory() as s:
        task = DownloadTask(tenant_id=1, project_id=1, owner_user_id=1,
                            repo_id="o/r", revision="abc", storage_id=1,
                            path_template="t", status="scheduling")
        s.add(task)
        await s.flush()
        for f in _files():
            s.add(FileSubTask(task_id=task.id, tenant_id=1, filename=f.filename,
                              file_size=f.size, expected_sha256=f.sha256,
                              status="pending"))
        await s.commit()
        reg = _FakeReg({"huggingface": _FakeDriver("huggingface", _files(),
                                                   True),
                        "modelscope": _FakeDriver("modelscope", _files(),
                                                  False)})
        await plan_task_sources(
            s, task, registry=reg, resolver=_IdResolver(),
            speeds={("huggingface"): 50.0, ("modelscope"): 900.0},
            chunk_min_mb=100)
        await s.commit()
        subs = (await s.execute(select(FileSubTask).where(
            FileSubTask.task_id == task.id))).scalars().all()
        assert all(x.source_id in {"huggingface", "modelscope"} for x in subs)
        big = next(x for x in subs if x.filename == "model.safetensors")
        assert big.is_chunked is True
        chunks = (await s.execute(select(SubtaskChunk).where(
            SubtaskChunk.subtask_id == big.id))).scalars().all()
        assert len(chunks) >= 2


async def test_hf_absent_pauses_when_not_trusted(factory):
    async with factory() as s:
        task = DownloadTask(tenant_id=1, project_id=1, owner_user_id=1,
                            repo_id="o/r", revision="abc", storage_id=1,
                            path_template="t", status="scheduling",
                            trust_non_hf_sha256=False)
        s.add(task)
        await s.flush()
        s.add(FileSubTask(task_id=task.id, tenant_id=1, filename="c.json",
                          file_size=10, expected_sha256=None,
                          status="pending"))
        await s.commit()
        reg = _FakeReg({"modelscope": _FakeDriver("modelscope", _files(),
                                                  False)})
        await plan_task_sources(s, task, registry=reg, resolver=_IdResolver(),
                                speeds={"modelscope": 900.0}, chunk_min_mb=100)
        await s.commit()
        assert task.status == "paused_external"
        assert task.error_message == "no_sha256_authority"


async def test_no_sha_file_pinned_to_huggingface(factory):
    """INVARIANT 12 (spec ruling 6a): a file with expected_sha256=None must
    stay on huggingface even when a faster non-HF source covers it."""
    async with factory() as s:
        task = DownloadTask(tenant_id=1, project_id=1, owner_user_id=1,
                            repo_id="o/r", revision="abc", storage_id=1,
                            path_template="t", status="scheduling")
        s.add(task)
        await s.flush()
        s.add(FileSubTask(task_id=task.id, tenant_id=1,
                          filename="config.json", file_size=10,
                          expected_sha256=None, status="pending"))
        await s.commit()
        reg = _FakeReg({"huggingface": _FakeDriver("huggingface", _files(),
                                                   True),
                        "modelscope": _FakeDriver("modelscope", _files(),
                                                  False)})
        await plan_task_sources(s, task, registry=reg, resolver=_IdResolver(),
                                speeds={"huggingface": 1.0,
                                        "modelscope": 9000.0},
                                chunk_min_mb=100)
        await s.commit()
        sub = (await s.execute(select(FileSubTask).where(
            FileSubTask.task_id == task.id))).scalar_one()
        assert sub.source_id == "huggingface" and sub.is_chunked is False


async def test_pin_modelscope_unreachable_pauses(factory):
    async with factory() as s:
        task = DownloadTask(tenant_id=1, project_id=1, owner_user_id=1,
                            repo_id="o/r", revision="abc", storage_id=1,
                            path_template="t", status="scheduling",
                            source_strategy="pin_modelscope")
        s.add(task)
        await s.flush()
        s.add(FileSubTask(task_id=task.id, tenant_id=1, filename="m",
                          file_size=10, expected_sha256="a" * 64,
                          status="pending"))
        await s.commit()
        reg = _FakeReg({"huggingface": _FakeDriver("huggingface", _files(),
                                                   True)})  # no modelscope
        await plan_task_sources(s, task, registry=reg, resolver=_IdResolver(),
                                speeds={"huggingface": 50.0}, chunk_min_mb=100)
        await s.commit()
        assert task.status == "paused_external"
        assert task.error_message == "pinned_source_unavailable"
```

- [ ] **Step 2: Run** `uv run pytest tests/services/test_source_scheduler.py -v` → FAIL.

- [ ] **Step 3: Implement** — `src/dlw/services/source_scheduler.py`:
```python
"""Task scheduling-phase source planner (Phase 3 SP2; doc §1.6/§1.8).
Caller commits."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.source import SubtaskChunk
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.services.source_combo import assign_files_lpt, solve_optimal_combo

_CHUNK_BYTES = 64 * 1024 * 1024   # source-routing chunk granularity


def _strategy_filter(enabled: list[str], strategy: str,
                     blacklist: list[str]) -> tuple[list[str], str | None]:
    """Apply task.source_strategy + task.source_blacklist (spec ruling 6e).
    Returns (allowed_ids, pinned_or_None). pinned!=None means an explicit
    single-source pin that must be honored (pause if unreachable)."""
    allowed = [s for s in enabled if s not in blacklist]
    if strategy == "auto_balance" or not strategy:
        return allowed, None
    if strategy == "fastest_only":
        return allowed, None              # combo will pick the single fastest
    if strategy.startswith("pin_"):
        pin = strategy.removeprefix("pin_")
        return ([pin] if pin in allowed else []), pin
    if strategy.startswith("list:"):
        wanted = [x.strip() for x in strategy.removeprefix("list:").split(",")]
        return [s for s in allowed if s in wanted], None
    return allowed, None


async def plan_task_sources(
    session: AsyncSession, task: DownloadTask, *,
    registry: Any, resolver: Any, speeds: dict[str, float],
    chunk_min_mb: int, overhead_pct: float = 2.0,
) -> None:
    # 1. apply source_strategy / source_blacklist (spec ruling 6e)
    allowed, pinned = _strategy_filter(
        registry.enabled_ids(), task.source_strategy or "auto_balance",
        list(task.source_blacklist or []))

    # 2. resolve manifests across allowed sources
    manifests: dict[str, Any] = {}
    for sid in allowed:
        drv = registry.get(sid)
        src_repo = resolver.resolve(sid, task.repo_id)
        if src_repo is None:
            continue
        m = await drv.resolve(src_repo, task.revision)
        if m is not None:
            manifests[sid] = (drv, m)

    if pinned is not None and pinned not in manifests:
        task.status = "paused_external"
        task.error_message = "pinned_source_unavailable"
        return

    # 3. HF sha256 authority gate (INVARIANT 13)
    hf_ok = "huggingface" in manifests
    if not hf_ok and not task.trust_non_hf_sha256:
        task.status = "paused_external"
        task.error_message = "no_sha256_authority"
        return

    # 4. candidates = covering sources with positive speed (spec ruling 6c)
    candidates = {sid: speeds[sid] for sid in manifests
                  if sid in speeds and speeds[sid] > 0}
    if not candidates:
        task.status = "paused_external"
        task.error_message = "no_source_speed"
        return
    subs = (await session.execute(select(FileSubTask).where(
        FileSubTask.task_id == task.id))).scalars().all()
    sizes = {x.filename: (x.file_size or 0) for x in subs}
    combo = solve_optimal_combo(candidates, sizes, overhead_pct=overhead_pct)
    combo_speeds = {s: candidates[s] for s in combo}

    # 5. assign; INVARIANT 12 — files with no HF sha authority stay HF-only
    assign = assign_files_lpt(sizes, combo_speeds)
    hf_files: set[str] = set()
    if "huggingface" in manifests:
        hf_files = {f.filename for f in manifests["huggingface"][1].files}
    chunk_min = chunk_min_mb * 1024 * 1024
    for sub in subs:
        no_hf_authority = (sub.expected_sha256 is None
                           or sub.filename not in hf_files)
        if no_hf_authority and not task.trust_non_hf_sha256:
            if "huggingface" not in manifests:
                task.status = "paused_external"
                task.error_message = "no_sha256_authority"
                return
            sub.source_id = "huggingface"      # single-source, no chunk-split
            continue
        sid = assign[sub.filename]
        sub.source_id = sid
        covering = [s for s in combo
                    if any(f.filename == sub.filename
                           for f in manifests[s][1].files)]
        if (sub.file_size or 0) >= chunk_min and len(covering) >= 2:
            sub.is_chunked = True
            await _split_chunks(session, sub, sub.file_size, covering,
                                combo_speeds)


async def _split_chunks(
    session: AsyncSession, sub: FileSubTask, size: int,
    sources: list[str], speeds: dict[str, float],
) -> None:
    total = sum(speeds[s] for s in sources) or 1.0
    offset = 0
    idx = 0
    for i, sid in enumerate(sources):
        if i == len(sources) - 1:
            length = size - offset
        else:
            portion = int(size * speeds[sid] / total)
            length = max(_CHUNK_BYTES,
                         (portion // _CHUNK_BYTES) * _CHUNK_BYTES)
            length = min(length, size - offset)
        if length <= 0:
            continue
        session.add(SubtaskChunk(
            subtask_id=sub.id, chunk_index=idx, byte_start=offset,
            byte_end=offset + length - 1, source_id=sid, status="pending"))
        offset += length
        idx += 1
```

- [ ] **Step 4: Run** `uv run pytest tests/services/test_source_scheduler.py -v` → 4 PASS.

- [ ] **Step 5: Commit**
```bash
git add src/dlw/services/source_scheduler.py tests/services/test_source_scheduler.py
git commit -m "feat(sp2): plan_task_sources (resolve/assign/chunk-split + HF gate)"
```

---

# Milestone M4 — Proxy + Executor + Lifespan

### Task 12: Generalized source-proxy

**Files:** Create `src/dlw/api/source_proxy.py`; Modify `src/dlw/main.py` (mount router); Test `tests/api/test_source_proxy.py`

- [ ] **Step 1: Write the failing test** — `tests/api/test_source_proxy.py`:
```python
"""source-proxy routes to the assigned driver, INVARIANT 2 (SP2)."""
from __future__ import annotations

import uuid

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

import dlw.db.models  # noqa: F401
from dlw.db.base import Base
from tests.conftest import make_app_with_state, register_test_executor

pytestmark = pytest.mark.slow

SECRET = "unit-secret"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", SECRET)
    from dlw.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def app_client(ephemeral_ca, engine):
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)
        await c.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.task import DownloadTask, FileSubTask
    from dlw.db.models.tenant import Project, Tenant, User
    async with f() as s:
        s.add(Tenant(id=1, slug="t", display_name="T"))
        await s.flush()
        s.add_all([Project(id=1, tenant_id=1, name="d"),
                   User(id=1, tenant_id=1, oidc_subject="u", email="e",
                        role="tenant_operator"),
                   StorageBackend(id=1, tenant_id=1, name="s",
                                  backend_type="s3", config_encrypted=b"")])
        await s.commit()
    app = make_app_with_state(ephemeral_ca, enrollment_token="e")

    # fake registry on app.state: a driver that streams "HELLO"
    class _D:
        id = "modelscope"

        def download_url(self, file):
            return "https://www.modelscope.cn/x"

        def auth_token(self, t):
            from dlw.sources.base import SourceToken
            return SourceToken(scheme="none")

    class _Reg:
        def get(self, sid):
            return _D() if sid == "modelscope" else None

    app.state.source_registry = _Reg()
    # patch the proxy's outbound client to a MockTransport
    import dlw.api.source_proxy as sp

    def _mk(_t):
        return httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=b"HELLO",
                                     headers={"Content-Length": "5"})))
    monkeypatch_target = sp
    sp._make_source_client = _mk  # type: ignore[attr-defined]

    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield app, c, f


async def test_proxy_streams_from_assigned_source(app_client):
    app, client, f = app_client
    from dlw.db.models.task import DownloadTask, FileSubTask
    reg = await register_test_executor(client, enrollment_token="e")
    async with f() as s:
        t = DownloadTask(tenant_id=1, project_id=1, owner_user_id=1,
                         repo_id="o/r", revision="abc", storage_id=1,
                         path_template="t", status="downloading")
        s.add(t)
        await s.flush()
        tok = uuid.uuid4()
        sub = FileSubTask(task_id=t.id, tenant_id=1, filename="m",
                          file_size=5, status="assigned",
                          executor_id=reg["executor_id"],
                          executor_epoch=reg["epoch"], assignment_token=tok,
                          source_id="modelscope")
        s.add(sub)
        await s.commit()
        sub_id = sub.id
    from tests.conftest import executor_request_headers
    h = {**executor_request_headers(reg), "X-Assignment-Token": str(tok)}
    r = await client.get(f"/api/v1/source-proxy/subtask/{sub_id}", headers=h)
    assert r.status_code == 200
    assert r.content == b"HELLO"
```

- [ ] **Step 2: Run** `uv run pytest tests/api/test_source_proxy.py -v` → FAIL.

- [ ] **Step 3: Implement** — `src/dlw/api/source_proxy.py` (copy the W3b ownership chain from `src/dlw/api/hf_proxy.py`, swap URL building for driver dispatch):
```python
"""Generalized multi-source reverse-proxy (Phase 3 SP2). Mirrors the W3b
hf_proxy ownership chain; routes each subtask/chunk to its assigned
SourceDriver and injects that source's controller-side credential. The
source token NEVER leaves the controller (INVARIANT 2)."""
from __future__ import annotations

import uuid

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.api.tasks import _session
from dlw.auth.executor_jwt_dep import require_executor_jwt
from dlw.config import get_settings
from dlw.db.models.executor import Executor
from dlw.db.models.source import SubtaskChunk
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.sources.base import SourceFile

router = APIRouter(prefix="/api/v1/source-proxy", tags=["executors"])

_HDR_ALLOW = frozenset({
    "content-length", "content-range", "content-type",
    "accept-ranges", "etag",
})


def _make_source_client(timeout_seconds: int) -> httpx.AsyncClient:
    """Test seam — monkeypatched to inject httpx.MockTransport."""
    return httpx.AsyncClient(follow_redirects=True, timeout=timeout_seconds)


@router.get("/subtask/{subtask_id}")
async def source_proxy_subtask(
    subtask_id: uuid.UUID,
    request: Request,
    x_assignment_token: str = Header(..., alias="X-Assignment-Token"),
    auth_ex: Executor = Depends(require_executor_jwt),
    session: AsyncSession = Depends(_session),
) -> StreamingResponse:
    sub = await session.get(FileSubTask, subtask_id)
    if sub is None:
        raise HTTPException(404, detail="subtask not found")
    if sub.executor_id != auth_ex.id:
        raise HTTPException(403, detail={"code": "NOT_YOUR_SUBTASK"})
    if sub.assignment_token is None or str(sub.assignment_token) != x_assignment_token:
        raise HTTPException(409, detail={"code": "STALE_ASSIGNMENT"})
    if sub.executor_epoch != auth_ex.epoch:
        raise HTTPException(409, detail={"code": "EPOCH_MISMATCH"})
    task = await session.get(DownloadTask, sub.task_id)
    if task is None:
        raise HTTPException(500, detail="parent task missing")

    settings = get_settings()
    range_header = request.headers.get("Range")

    # which source? chunked subtask → resolve by Range start; else sub.source_id
    source_id = sub.source_id
    if sub.is_chunked and range_header and range_header.startswith("bytes="):
        start = int(range_header.split("=", 1)[1].split("-", 1)[0])
        chunk = await session.scalar(select(SubtaskChunk).where(
            SubtaskChunk.subtask_id == sub.id,
            SubtaskChunk.byte_start <= start,
            SubtaskChunk.byte_end >= start))
        if chunk is not None:
            source_id = chunk.source_id
    if source_id is None:
        raise HTTPException(409, detail={"code": "SOURCE_UNASSIGNED"})

    registry = request.app.state.source_registry
    drv = registry.get(source_id)
    if drv is None:
        raise HTTPException(502, detail={"code": "SOURCE_UNAVAILABLE"})

    src_file = SourceFile(filename=sub.filename, size=sub.file_size,
                          sha256=sub.expected_sha256,
                          download_ref=f"{task.repo_id}/resolve/"
                                       f"{task.revision}/{sub.filename}")
    url = drv.download_url(src_file)
    tok = drv.auth_token(settings.hf_token)
    headers: dict[str, str] = {}
    if tok.scheme == "bearer" and tok.value:
        headers["Authorization"] = f"Bearer {tok.value}"
    if range_header:
        headers["Range"] = range_header

    client = _make_source_client(settings.hf_proxy_timeout_seconds)
    req = client.build_request("GET", url, headers=headers)
    try:
        resp = await client.send(req, stream=True)
    except (httpx.TimeoutException, httpx.NetworkError) as e:
        await client.aclose()
        raise HTTPException(503, detail=f"source unreachable: {e}") from e
    except BaseException:
        await client.aclose()
        raise

    fwd = {k: v for k, v in resp.headers.items()
           if k.lower() in _HDR_ALLOW}

    async def _body():
        try:
            async for chunk in resp.aiter_bytes(64 * 1024):
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(_body(), status_code=resp.status_code,
                             headers=fwd)
```
In `src/dlw/main.py` `create_app()` (after the existing router includes), add:
```python
    from dlw.api.source_proxy import router as source_proxy_router
    app.include_router(source_proxy_router)
```

- [ ] **Step 4: Run** `uv run pytest tests/api/test_source_proxy.py -v` → PASS. (If the `monkeypatch` seam in the fixture needs the `monkeypatch` fixture arg, add it to `app_client(ephemeral_ca, engine, monkeypatch)` and use `monkeypatch.setattr(sp, "_make_source_client", _mk)` — adjust during impl; the seam is `dlw.api.source_proxy._make_source_client`.)

- [ ] **Step 5: Commit**
```bash
git add src/dlw/api/source_proxy.py src/dlw/main.py tests/api/test_source_proxy.py
git commit -m "feat(sp2): generalized /source-proxy with per-source cred (INV 2)"
```

---

### Task 13: Executor `stream_source`

**Files:** Modify `src/dlw/executor/client.py`, `src/dlw/executor/chunk_downloader.py`, `src/dlw/executor/downloader.py`, `tests/conftest.py`; Test `tests/executor/test_stream_source.py`

- [ ] **Step 1: Write the failing test** — `tests/executor/test_stream_source.py`:
```python
"""ControllerClient.stream_source targets /source-proxy (Phase 3 SP2)."""
from __future__ import annotations

import uuid

import httpx
import pytest

from dlw.executor.client import ControllerClient
from tests.conftest import make_fake_auth_state


async def test_stream_source_hits_source_proxy(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["range"] = request.headers.get("Range")
        return httpx.Response(200, content=b"DATA")

    c = ControllerClient(
        "http://ctrl",
        auth_state=make_fake_auth_state(tmp_path),
        _transport=httpx.MockTransport(handler))
    sid = uuid.uuid4()
    tok = uuid.uuid4()
    async with c.stream_source(subtask_id=sid, assignment_token=tok,
                               range_header="bytes=0-3") as resp:
        assert resp.status_code == 200
        body = b""
        async for b in resp.aiter_bytes():
            body += b
    assert body == b"DATA"
    assert seen["path"] == f"/api/v1/source-proxy/subtask/{sid}"
    assert seen["range"] == "bytes=0-3"
```

- [ ] **Step 2: Run** `uv run pytest tests/executor/test_stream_source.py -v` → FAIL.

- [ ] **Step 3: Implement** — in `src/dlw/executor/client.py`, add a method mirroring `stream_hf` exactly but with the source-proxy path (place directly after `stream_hf`):
```python
    @asynccontextmanager
    async def stream_source(
        self,
        *,
        subtask_id: uuid.UUID,
        assignment_token: uuid.UUID,
        range_header: str | None = None,
    ) -> AsyncIterator[httpx.Response]:
        """SP2: stream a file/chunk from its assigned source via the
        controller's generalized reverse-proxy. Same contract as stream_hf
        (caller inspects resp.status_code; no raise_for_status)."""
        headers = {
            **self._auth_headers(),
            "X-Assignment-Token": str(assignment_token),
        }
        if range_header:
            headers["Range"] = range_header
        async with self._make_client() as client:
            async with client.stream(
                "GET",
                f"/api/v1/source-proxy/subtask/{subtask_id}",
                headers=headers,
            ) as resp:
                yield resp
```
In `src/dlw/executor/chunk_downloader.py`, replace the **two** `self._controller.stream_hf(` call sites (verified: lines ~74 in `_resolve_size`, ~128 in `_download_one_chunk`) with `self._controller.stream_source(` (identical signature). In `src/dlw/executor/downloader.py`, replace its single `self._controller.stream_hf(` call site (~line 71, `HfS3StreamDownloader` — the non-chunked single-file path) with `self._controller.stream_source(` too, so EVERY executor download goes through `/source-proxy` and honors the planner's `sub.source_id` (an HF-assigned subtask is just routed to the `huggingface` driver — equivalent to the old `/hf-proxy`). Nothing else in those files changes.

**Chunk alignment (spec ruling 6b):** for an `is_chunked` subtask the executor must download one Range per `subtask_chunks` row, not its local `plan_chunks` split. Add to `DirectOffsetDownloader`: when the assignment indicates a chunked subtask, fetch the chunk rows from the controller and use their `byte_start/byte_end` as the chunk plan (each Range then maps to exactly one source in `source_proxy`). MINIMAL implementation for SP2: the controller exposes the chunk boundaries in the poll/assignment payload (a `chunks: [[start,end],...]` list when `is_chunked`); `DirectOffsetDownloader.download` uses those offsets instead of `plan_chunks(...)` when present. (If wiring the assignment payload is non-trivial, the implementer reports DONE_WITH_CONCERNS and the controller decides; the spec ruling 6b is the contract — every executor Range must align to one `subtask_chunks` row.) The existing sequential offset-order SHA256 in `_pass2_upload` is unchanged and remains the whole-file hash the W4 gate verifies.

In `tests/conftest.py`, the shared test double `make_fake_controller_client._FakeControllerClient` currently defines only `stream_hf`. Add a `stream_source` method to it that mirrors `stream_hf` exactly but targets `/api/v1/source-proxy/subtask/{subtask_id}` (same `@asynccontextmanager`/MockTransport body, same params). This keeps `tests/executor/test_chunk_downloader.py` + `tests/executor/test_downloader.py` green after the swap.

- [ ] **Step 4: Run** `uv run pytest tests/executor/test_stream_source.py tests/executor/test_chunk_downloader.py tests/executor/test_downloader.py -v` → PASS (the conftest `stream_source` addition keeps the existing downloader tests green; if any test asserts the literal `/hf-proxy` path, update that assertion to `/source-proxy`).

- [ ] **Step 5: Commit**
```bash
git add src/dlw/executor/client.py src/dlw/executor/chunk_downloader.py src/dlw/executor/downloader.py tests/conftest.py tests/executor/test_stream_source.py
git commit -m "feat(sp2): executor stream_source -> /source-proxy (all paths) + conftest fake"
```

---

### Task 14: Lifespan bootstrap + scheduling/rebalance loops + conftest

**Files:** Modify `src/dlw/main.py`, `tests/conftest.py`, `tests/test_lifespan_state.py`; Test `tests/test_sp2_lifespan.py`

- [ ] **Step 1: Write the failing test** — `tests/test_sp2_lifespan.py`:
```python
"""Real lifespan bootstraps source_registry + name_resolver (SP2;
SP1-regression-class: app.state used by routes MUST be set in lifespan)."""
from __future__ import annotations

import pytest

import dlw.db.models  # noqa: F401
from dlw.db.base import Base

pytestmark = pytest.mark.slow


async def test_lifespan_sets_source_state(engine, tmp_path, monkeypatch):
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    monkeypatch.setenv("DLW_AUTH_DEV_MODE", "true")
    monkeypatch.setenv("DLW_CA_DIR", str(tmp_path / "ca"))
    from dlw.config import get_settings
    get_settings.cache_clear()
    from dlw.main import create_app, lifespan
    from dlw.sources.registry import SourceRegistry
    app = create_app()
    async with lifespan(app):
        assert isinstance(app.state.source_registry, SourceRegistry)
        assert app.state.name_resolver is not None
        assert "huggingface" in app.state.source_registry.enabled_ids()
    get_settings.cache_clear()
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)
```

- [ ] **Step 2: Run** `uv run pytest tests/test_sp2_lifespan.py -v` → FAIL.

- [ ] **Step 3: Implement** — in `src/dlw/main.py` lifespan, in the **unconditional** SP1 block (right after `app.state.casbin = build_enforcer(grants=_grants)`), add:
```python
    from dlw.sources.name_resolver import NameResolver
    from dlw.sources.registry import load_registry
    app.state.source_registry = load_registry(
        _settings.sources_yaml_path, hf_token=_settings.hf_token)
    app.state.name_resolver = NameResolver.from_file(
        _settings.resolver_rules_path)
```
Add two leader-gated loops mirroring SP1's `_quota_loop`/`quota_task_holder` exactly. After the `quota_task_holder` definition add:
```python
    sched_task_holder: dict[str, asyncio.Task | None] = {"t": None}
    rebalance_task_holder: dict[str, asyncio.Task | None] = {"t": None}

    async def _scheduling_loop() -> None:
        from dlw.services.source_scheduler import run_scheduling_tick
        while True:
            try:
                await asyncio.sleep(5)
                async with factory() as session:
                    await run_scheduling_tick(
                        session, app.state.source_registry,
                        app.state.name_resolver, _gs())
                    await session.commit()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("scheduling tick failed; retrying")

    async def _rebalance_loop() -> None:
        from dlw.services.source_scheduler import run_rebalance_tick
        while True:
            try:
                await asyncio.sleep(_gs().rebalance_interval_seconds)
                async with factory() as session:
                    await run_rebalance_tick(session, _gs())
                    await session.commit()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("rebalance tick failed; retrying")
```
(`_gs` is the lifespan-local alias for `get_settings` — `from dlw.config import get_settings as _gs` at `main.py:57`; do NOT call a bare `get_settings()` here, it is not in scope and would `NameError`.)

In `_on_active` (next to the existing `quota_task_holder["t"] = ...`):
```python
        sched_task_holder["t"] = asyncio.create_task(_scheduling_loop())
        rebalance_task_holder["t"] = asyncio.create_task(_rebalance_loop())
```
In `_on_step_down`, after the existing quota-task cancel block, add the symmetric cancel for both new holders (mirror the exact `qt = ...; if qt is not None: qt.cancel(); try: await asyncio.wait_for(qt, timeout=2) ...; holder["t"] = None` block for `sched_task_holder` and `rebalance_task_holder`).
Add `run_scheduling_tick`/`run_rebalance_tick` to `src/dlw/services/source_scheduler.py`:
```python
async def run_scheduling_tick(session, registry, resolver, settings) -> None:
    """Pick `pending` tasks; controller-side probe each source; plan; move
    to claimable. Probe = one small ranged GET controller→source (spec
    ruling 6d); fused with latest SourceSpeedSample EWMA history."""
    from dlw.db.models.source import SourceSpeedSample
    from dlw.services.source_speed import (
        fuse_ewma,
        pick_probe_size_bytes,
        probe_source_speed,
    )
    pend = (await session.execute(select(DownloadTask).where(
        DownloadTask.status == "pending").limit(20))).scalars().all()
    probe_bytes = pick_probe_size_bytes(probe_size_mb=settings.probe_size_mb)
    for task in pend:
        task.status = "scheduling"
        speeds: dict[str, float] = {}
        for sid in registry.enabled_ids():
            drv = registry.get(sid)
            src_repo = resolver.resolve(sid, task.repo_id)
            live = 0.0
            if src_repo is not None:
                try:
                    m = await drv.resolve(src_repo, task.revision)
                except Exception:
                    m = None
                if m is not None and m.files:
                    probe_f = min(m.files, key=lambda f: f.size or 1 << 62)
                    live = await probe_source_speed(
                        drv, probe_f, probe_bytes=probe_bytes,
                        timeout_s=settings.probe_timeout_s,
                        hf_token=settings.hf_token)
            hist = await session.scalar(
                select(SourceSpeedSample.bytes_per_sec)
                .where(SourceSpeedSample.source_id == sid)
                .order_by(SourceSpeedSample.measured_at.desc()).limit(1))
            fused = fuse_ewma(live=live, hist=float(hist) if hist else None,
                              hist_weight=settings.probe_history_weight)
            if live > 0:
                session.add(SourceSpeedSample(
                    executor_id="controller", source_id=sid,
                    bytes_per_sec=live, sample_size=probe_bytes,
                    is_active_probe=True))
            speeds[sid] = fused if fused > 0 else 0.0
        await plan_task_sources(
            session, task, registry=registry, resolver=resolver,
            speeds=speeds, chunk_min_mb=settings.chunk_level_min_file_mb,
            overhead_pct=settings.combo_overhead_per_source_pct)
        if task.status == "scheduling":
            task.status = "downloading"


async def run_rebalance_tick(session, settings) -> None:
    """Reassign a degraded source's PENDING chunks to a healthy sibling
    source on the same subtask (in-flight chunks untouched)."""
    from sqlalchemy import text
    from dlw.services.source_blacklist import active_blacklisted_sources
    bad = await active_blacklisted_sources(session)
    if not bad:
        return
    for sub_src in bad:
        await session.execute(text(
            "UPDATE subtask_chunks c SET source_id = ("
            "  SELECT source_id FROM subtask_chunks d "
            "  WHERE d.subtask_id=c.subtask_id AND d.source_id!=:bad "
            "  LIMIT 1) "
            "WHERE c.source_id=:bad AND c.status='pending' "
            "AND EXISTS (SELECT 1 FROM subtask_chunks e "
            "  WHERE e.subtask_id=c.subtask_id AND e.source_id!=:bad)"
        ), {"bad": sub_src})
```
Add to `src/dlw/services/source_blacklist.py`:
```python
async def active_blacklisted_sources(session: AsyncSession) -> list[str]:
    rows = await session.execute(select(SourceBlacklist.source_id).where(
        SourceBlacklist.until > datetime.now(UTC)).distinct())
    return [r[0] for r in rows]
```
In `tests/conftest.py` `make_app_with_state`, after the `app.state.casbin = ...` line, add:
```python
    from dlw.sources.name_resolver import NameResolver
    from dlw.sources.registry import load_registry
    _s = app.state.settings
    app.state.source_registry = load_registry(
        _s.sources_yaml_path, hf_token=_s.hf_token)
    app.state.name_resolver = NameResolver.from_file(_s.resolver_rules_path)
```
(The default `config/sources.yaml`/`config/resolver-rules.yaml` exist from M1; tests run from repo root so the relative paths resolve.) Extend `tests/test_lifespan_state.py`'s existing test to also `assert app.state.source_registry is not None`.

- [ ] **Step 4: Run** `uv run pytest tests/test_sp2_lifespan.py tests/test_lifespan_state.py -v` → PASS.

- [ ] **Step 5: Commit**
```bash
git add src/dlw/main.py src/dlw/services/source_scheduler.py src/dlw/services/source_blacklist.py tests/conftest.py tests/test_lifespan_state.py tests/test_sp2_lifespan.py
git commit -m "feat(sp2): lifespan registry/resolver bootstrap + scheduling/rebalance loops"
```

---

### Task 15: SHA256 authority gate on report

**Files:** Modify `src/dlw/services/scheduler.py`; Test `tests/services/test_sha_authority.py`

- [ ] **Step 1: Write the failing test** — `tests/services/test_sha_authority.py`:
```python
"""Non-HF completion verified vs HF expected_sha256 → blacklist on mismatch."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import dlw.db.models  # noqa: F401
from dlw.db.base import Base
from dlw.db.models.source import SourceBlacklist
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.services.scheduler import complete_subtask

pytestmark = pytest.mark.slow


@pytest.fixture
async def factory(engine):
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)
        await c.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Project, Tenant, User
    async with f() as s:
        s.add(Tenant(id=1, slug="t", display_name="T"))
        await s.flush()
        s.add_all([Project(id=1, tenant_id=1, name="d"),
                   User(id=1, tenant_id=1, oidc_subject="u", email="e",
                        role="tenant_operator"),
                   StorageBackend(id=1, tenant_id=1, name="s",
                                  backend_type="s3", config_encrypted=b"")])
        await s.commit()
    yield f
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)


async def test_non_hf_sha_mismatch_blacklists(factory):
    async with factory() as s:
        t = DownloadTask(tenant_id=1, project_id=1, owner_user_id=1,
                          repo_id="o/r", revision="abc", storage_id=1,
                          path_template="t", status="downloading")
        s.add(t)
        await s.flush()
        tok = uuid.uuid4()
        sub = FileSubTask(task_id=t.id, tenant_id=1, filename="m",
                          file_size=4, expected_sha256="c" * 64,
                          status="assigned", assignment_token=tok,
                          source_id="modelscope")
        s.add(sub)
        await s.flush()
        sid = sub.id
        done, _ = await complete_subtask(
            s, sid, final_status="succeeded", actual_sha256="d" * 64,
            bytes_downloaded=4, error=None, assignment_token=tok)
        await s.commit()
        assert done.status == "failed"   # existing sha-gate already flips this
        bl = (await s.execute(select(SourceBlacklist).where(
            SourceBlacklist.source_id == "modelscope"))).scalars().all()
        assert len(bl) == 1 and bl[0].filename == "m"
```

- [ ] **Step 2: Run** `uv run pytest tests/services/test_sha_authority.py -v` → FAIL (no blacklist row written yet).

- [ ] **Step 3: Implement** — in `src/dlw/services/scheduler.py` `complete_subtask`: the existing W4 sha256 gate flips `final_status` to `"failed"` on mismatch (~lines 173-182), then `sub.status = final_status` (~185) and `parent = await session.get(DownloadTask, sub.task_id, with_for_update=True)` (~line 192-194). Insert the blacklist write **immediately AFTER that `parent = await session.get(...)` line** (so it reuses the already-locked `parent` — no duplicate fetch) and **before** the siblings query (~line 195). Add a module-top import `from dlw.services.source_blacklist import blacklist_file` (no circular import: `source_blacklist` imports only models; `scheduler` is not scanned for this). Insert exactly:
```python
    if (final_status == "failed" and sub.source_id
            and sub.source_id != "huggingface"
            and sub.expected_sha256 is not None
            and actual_sha256 != sub.expected_sha256):
        await blacklist_file(
            session, source_id=sub.source_id, repo_id=parent.repo_id,
            filename=sub.filename, hours=24, reason="sha_mismatch")
```
(`parent` here is the locked row already fetched on the preceding line — do NOT add a second `session.get`. `sub.status` is already `"failed"`. Re-queue/HF-repin of the file is handled by the next scheduling pass — this hook is intentionally minimal: just the 24h blacklist row, exactly what `tests/e2e/test_multi_source.py::test_sha256_mismatch_blacklists_source` and Task 15's test assert. Note: the only new string literals added to the scanned `scheduler.py` are `"failed"`/`"huggingface"` inside an `if` condition — NOT a `status=`/`.status =` assignment — so `tools/lint_invariants.py` does not flag them; confirm with `python tools/lint_invariants.py`.)

- [ ] **Step 4: Run** `uv run pytest tests/services/test_sha_authority.py -v` → PASS. Then `python tools/lint_invariants.py` → exit 0 (no new status literals added to scanned files; `"failed"`/`"huggingface"` are not status-kwarg literals flagged by the AST check — confirm).

- [ ] **Step 5: Commit**
```bash
git add src/dlw/services/scheduler.py tests/services/test_sha_authority.py
git commit -m "feat(sp2): HF sha256 authority — non-HF mismatch blacklists source 24h"
```

---

# Milestone M5 — E2E + Docs + PR

### Task 16: E2E-002 multi-source

**Files:** Create `tests/e2e/test_multi_source.py`

- [ ] **Step 1: Write the test** — `tests/e2e/test_multi_source.py`:
```python
"""E2E-002: auto_balance planning + HF-authority pause (Phase 3 SP2).

End-to-end at the planner+DB level (no live mirrors): a task with HF + a
faster ModelScope-style fake source gets files assigned to the faster
source, and an HF-absent task without trust pauses (INVARIANT 13)."""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import dlw.db.models  # noqa: F401
from dlw.db.base import Base
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.services.source_scheduler import plan_task_sources
from dlw.sources.base import SourceFile, SourceManifest

pytestmark = pytest.mark.slow


class _Drv:
    def __init__(self, sid, files):
        self.id = sid
        self.provides_sha256 = sid in ("huggingface", "hf_mirror")
        self._f = files

    async def resolve(self, repo, rev):
        return SourceManifest(self.id, repo, rev, self._f,
                              has_lfs_sha256=any(f.sha256 for f in self._f))


class _Reg:
    def __init__(self, d):
        self._d = d

    def enabled_ids(self):
        return list(self._d)

    def get(self, s):
        return self._d.get(s)


class _Id:
    def resolve(self, sid, repo):
        return repo


@pytest.fixture
async def factory(engine):
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)
        await c.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    from dlw.db.models.storage import StorageBackend
    from dlw.db.models.tenant import Project, Tenant, User
    async with f() as s:
        s.add(Tenant(id=1, slug="t", display_name="T"))
        await s.flush()
        s.add_all([Project(id=1, tenant_id=1, name="d"),
                   User(id=1, tenant_id=1, oidc_subject="u", email="e",
                        role="tenant_operator"),
                   StorageBackend(id=1, tenant_id=1, name="s",
                                  backend_type="s3", config_encrypted=b"")])
        await s.commit()
    yield f
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)


async def test_auto_balance_prefers_fast_source(factory):
    files = [SourceFile("a.safetensors", 50, "a" * 64, "r"),
             SourceFile("b.safetensors", 50, "b" * 64, "r")]
    reg = _Reg({"huggingface": _Drv("huggingface", files),
                "modelscope": _Drv("modelscope", files)})
    async with factory() as s:
        t = DownloadTask(tenant_id=1, project_id=1, owner_user_id=1,
                         repo_id="o/r", revision="abc", storage_id=1,
                         path_template="t", status="scheduling")
        s.add(t)
        await s.flush()
        for f in files:
            s.add(FileSubTask(task_id=t.id, tenant_id=1, filename=f.filename,
                              file_size=f.size, expected_sha256=f.sha256,
                              status="pending"))
        await s.commit()
        await plan_task_sources(s, t, registry=reg, resolver=_Id(),
                                speeds={"huggingface": 50.0,
                                        "modelscope": 5000.0},
                                chunk_min_mb=100)
        await s.commit()
        subs = (await s.execute(select(FileSubTask).where(
            FileSubTask.task_id == t.id))).scalars().all()
        assert all(x.source_id == "modelscope" for x in subs)  # HF too slow


async def test_hf_unavailable_pauses(factory):
    files = [SourceFile("a", 10, None, "r")]
    reg = _Reg({"modelscope": _Drv("modelscope", files)})
    async with factory() as s:
        t = DownloadTask(tenant_id=1, project_id=1, owner_user_id=1,
                         repo_id="o/r", revision="abc", storage_id=1,
                         path_template="t", status="scheduling",
                         trust_non_hf_sha256=False)
        s.add(t)
        await s.flush()
        s.add(FileSubTask(task_id=t.id, tenant_id=1, filename="a",
                          file_size=10, status="pending"))
        await s.commit()
        await plan_task_sources(s, t, registry=reg, resolver=_Id(),
                                speeds={"modelscope": 900.0}, chunk_min_mb=100)
        await s.commit()
        assert t.status == "paused_external"
        assert t.error_message == "no_sha256_authority"
```

- [ ] **Step 2: Run** `uv run pytest tests/e2e/test_multi_source.py -v` → 2 PASS (planner + gate already implemented; this is the integration acceptance gate — if it fails, fix the underlying code, not the test).

- [ ] **Step 3: Commit**
```bash
git add tests/e2e/test_multi_source.py
git commit -m "test(sp2): E2E-002 auto_balance prefers fast source + HF-authority pause"
```

---

### Task 17: OpenAPI + operator doc + full CI gates + PR

**Files:** Modify `api/openapi.yaml`; Create `docs/operator/multi-source.md`

- [ ] **Step 1: Update `api/openapi.yaml`** — add the `GET /api/v1/source-proxy/subtask/{subtaskId}` operation (tag `executors`, `X-Assignment-Token` header + optional `Range`, 200 stream / 403 `NOT_YOUR_SUBTASK` / 409 `STALE_ASSIGNMENT`|`EPOCH_MISMATCH` / 502 `SOURCE_UNAVAILABLE` / 503), mirroring the existing `/api/v1/hf-proxy/subtask/{subtaskId}` operation's structure exactly. Add the `source_strategy`/`source_blacklist`/`trust_non_hf_sha256` properties to the `TaskCreate` request schema and `scheduling` to the task `status` enum if one is defined. Match existing indentation/style.

- [ ] **Step 2: Run the exact OpenAPI CI commands** (no code-vs-yaml gate):
```bash
npx --yes @stoplight/spectral-cli@6 lint api/openapi.yaml --fail-severity=error
npx --yes @apidevtools/swagger-cli validate api/openapi.yaml
```
Both must pass (spectral: 0 errors; warnings OK). Also `npx --yes yaml-lint api/openapi.yaml` style sanity — keep 2-space indent, no trailing whitespace (the `yamllint` CI job scans `api/`).

- [ ] **Step 3: Create `docs/operator/multi-source.md`** (~100 lines): `config/sources.yaml` schema (id/enabled/driver/config/cost; supported drivers = huggingface/hf_mirror/modelscope; others ignored), `config/resolver-rules.yaml` (identity_organizations / aliases transform / per_model_overrides with examples), the SP2 `DLW_*` settings (probe/chunk/blacklist/rebalance), `source_strategy` task field values, the HF-sha256-authority rule (INVARIANT 13: HF-down → `paused_external` unless `trust_non_hf_sha256`), the 24h sha-mismatch blacklist, and that scheduling/rebalance run only on the active controller (leader-gated). Cross-ref `docs/v2.0/06-platform-and-ecosystem.md` §1 and `INVARIANTS` 11/12/13.

- [ ] **Step 4: Full suite + all real CI gates locally**:
```bash
uv lock && uv sync --all-groups
uv run pytest tests/ --cov=src/dlw --cov-report=term-missing
uv run python -m pytest tools/test_lint_invariants.py -v
python tools/lint_invariants.py
python tools/lint_no_direct_status_write.py
```
All green. (uv.lock already committed in Task 1; re-run `uv lock` only if drift.) Confirm `python tools/lint_invariants.py` exits 0 — SP2 added no status literals to the 3 scanned files (chunk statuses live in `source_scheduler.py`/`source_proxy.py`; `"scheduling"` is already in `VALID_TASK_STATUS`).

- [ ] **Step 5: Commit + push + PR**
```bash
git add api/openapi.yaml docs/operator/multi-source.md
git commit -m "docs(sp2): OpenAPI source-proxy op + operator multi-source guide"
git push -u origin feat/phase-3-sp2-multi-source
gh pr create --title "Phase 3 SP2 — Multi-source (SourceDriver + NameResolver + LPT/chunk routing)" --body "$(cat <<'EOF'
## Summary
- SourceDriver Protocol + HF/hf_mirror/ModelScope drivers + sources.yaml registry + NameResolver (resolver-rules.yaml).
- Leader-gated scheduling loop: resolve → optimal-combo → LPT file→source + chunk-split (≥100MB, ≥2 sources) → persist source_id/subtask_chunks. HF sha256 authority (INV 11/12/13): HF-down→paused_external unless trust flag; non-HF mismatch → 24h source blacklist.
- Generalized /api/v1/source-proxy (per-source cred stays controller-side, INV 2); executor stream_source; minimal leader-gated rebalance of degraded sources' pending chunks.
- Additive migration (3 tables + task/subtask source columns). Phase 3 sub-project 2 of 4 (SP1 merged #15).

## Test plan
- [ ] full `uv run pytest` green incl. E2E-002 `tests/e2e/test_multi_source.py`
- [ ] invariant_lint / openapi(spectral+swagger-cli) / yamllint CI gates green
- [ ] alembic up/down/up clean; uv.lock committed (pyyaml)

Spec: docs/superpowers/specs/2026-05-19-phase-3-sp2-multi-source-design.md
Plan: docs/superpowers/plans/2026-05-19-phase-3-sp2-multi-source.md

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review (completed during planning + to be re-checked by 2 pre-execution reviewers)

**Spec coverage:** SourceDriver/dataclasses→T2; HF/hf_mirror→T3; ModelScope→T4; registry/sources.yaml→T5; NameResolver/resolver-rules→T6; models+migration+TaskCreate fields→T7; speed EWMA→T8; LPT+combo→T9; blacklist→T10; planner+HF-authority gate+chunk-split→T11; source-proxy+INV2→T12; executor stream_source→T13; lifespan bootstrap+scheduling/rebalance loops+conftest+test_lifespan_state→T14; sha256-authority-on-report→T15; E2E-002→T16; OpenAPI+operator doc+CI+PR→T17. Deferred items (wisemodel/opencsg/plugin, Phase-B LP, UI, incremental=SP3, CLI=SP4) are explicitly out per spec §1.3/banner.

**Placeholder scan:** every code step has complete code. The one bounded judgement note (T12 Step 4 monkeypatch-seam fixture-arg detail) names the exact seam (`dlw.api.source_proxy._make_source_client`) and the fix; not a placeholder. `<rev>` = alembic-generated hash (intentional).

**Type/name consistency:** `SourceDriver`/`SourceManifest`/`SourceFile`/`SourceToken`/`SourceHealth` identical T2↔T3/4↔T5↔T11↔T12. `download_url(file)`+`auth_token(tenant_hf_token)` consistent T2↔T3/4↔T12. `load_registry(path, *, hf_token)`→`SourceRegistry.enabled_ids()/get()` consistent T5↔T11↔T12↔T14. `NameResolver.from_file(path)`/`.resolve(source_id, hf_repo_id)` consistent T6↔T11↔T14. `assign_files_lpt(files,speeds)`/`solve_optimal_combo(speeds,files,*,overhead_pct)` consistent T9↔T11. `plan_task_sources(session,task,*,registry,resolver,speeds,chunk_min_mb,overhead_pct)` consistent T11↔T14↔T16. `blacklist_file(...)`/`is_blacklisted(...)`/`active_blacklisted_sources(...)` consistent T10↔T14↔T15. `stream_source(*,subtask_id,assignment_token,range_header)` mirrors existing `stream_hf` T13↔T12. `SubtaskChunk`/`SourceSpeedSample`/`SourceBlacklist` columns consistent T7↔T10↔T11↔T14↔T15.

## References
- Spec: `docs/superpowers/specs/2026-05-19-phase-3-sp2-multi-source-design.md`
- Design doc: `docs/v2.0/06-platform-and-ecosystem.md` §1; Invariants 11/12/13.
- Code anchors: `src/dlw/services/hf_metadata.py` (`list_repo_tree`/`RepoFile`), `src/dlw/api/hf_proxy.py` (W3b ownership chain copied by source_proxy), `src/dlw/executor/chunk_downloader.py`/`client.py` (`stream_hf`→`stream_source`), `src/dlw/services/scheduler.py` `complete_subtask` (W4 sha gate), `tools/lint_invariants.py` (`scheduling` already in `VALID_TASK_STATUS`; only 3 files scanned), SP1's `main.py` `_quota_loop`/`make_app_with_state`/`test_lifespan_state` patterns, alembic head `a4bed702cdb3`.
- Branch `feat/phase-3-sp2-multi-source` off `main` (`fa08e6d`), spec `ccdb9e8`.
