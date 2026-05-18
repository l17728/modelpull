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
