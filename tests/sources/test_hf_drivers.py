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
