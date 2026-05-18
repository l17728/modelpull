"""hf-mirror.com SourceDriver — HF-compatible, no token, gated->skip (SP2)."""
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
            return None
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
