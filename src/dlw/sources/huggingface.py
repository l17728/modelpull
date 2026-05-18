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
        except RepoNotFound:
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
