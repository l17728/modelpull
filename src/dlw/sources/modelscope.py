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
