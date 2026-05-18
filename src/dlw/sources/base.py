"""SourceDriver abstraction (Phase 3 SP2; design doc 06 §1.3)."""
from __future__ import annotations

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
