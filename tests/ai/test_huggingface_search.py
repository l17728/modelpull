"""search_huggingface_models tool tests."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from dlw.ai.tools import READONLY_TOOLS


def _principal(uid: int = 1):
    from dlw.auth.principal import Principal
    return Principal(user_id=uid, tenant_id=1, role="tenant_operator",
                     project_ids=(), is_service=False)


def _mock_response(data, *, status_code: int = 200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json = MagicMock(return_value=data)
    return resp


SAMPLE = [
    {"id": "deepseek-ai/DeepSeek-V3", "downloads": 50000, "likes": 1200,
     "lastModified": "2026-04-24T10:00:00Z", "pipeline_tag": "text-generation",
     "tags": ["pytorch", "deepseek"]},
    {"id": "deepseek-ai/DeepSeek-R1", "downloads": 80000, "likes": 2000,
     "lastModified": "2026-03-15T08:00:00Z", "pipeline_tag": "text-generation",
     "tags": ["reasoning"]},
]


def _patched_client(get_result):
    """get_result is either a Response mock (used as return_value) OR an
    async function used as side_effect (must be a real coroutine function,
    not a MagicMock — MagicMock is callable and confuses the dispatcher)."""
    import inspect
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    if inspect.iscoroutinefunction(get_result):
        mock_client.get = AsyncMock(side_effect=get_result)
    else:
        mock_client.get = AsyncMock(return_value=get_result)
    return mock_client


async def test_tool_registered():
    assert "search_huggingface_models" in READONLY_TOOLS
    t = READONLY_TOOLS["search_huggingface_models"]
    assert "huggingface" in t.description.lower()
    assert "PREFER THIS over web_search" in t.description


async def test_happy_path_returns_models():
    with patch("dlw.ai.tools._httpx") as h:
        h.AsyncClient.return_value = _patched_client(_mock_response(SAMPLE))
        out = await READONLY_TOOLS["search_huggingface_models"].run(
            None, _principal(), query="deepseek")
    assert out["query"] == "deepseek"
    assert out["sort"] == "lastModified"
    assert out["total"] == 2
    assert out["results"][0]["id"].startswith("<external_user_content")
    assert "DeepSeek-V3" in out["results"][0]["id"]
    assert out["results"][0]["downloads"] == 50000
    assert out["results"][0]["likes"] == 1200
    assert out["results"][0]["pipeline_tag"] == "text-generation"
    assert "huggingface.co/deepseek-ai/DeepSeek-V3" in out["results"][0]["url"]


async def test_sort_parameter_forwarded():
    captured = {}
    async def _h(url, *, params, headers):
        captured["params"] = params
        return _mock_response(SAMPLE)
    with patch("dlw.ai.tools._httpx") as h:
        h.AsyncClient.return_value = _patched_client(_h)
        await READONLY_TOOLS["search_huggingface_models"].run(
            None, _principal(), query="qwen", sort="downloads")
    assert captured["params"]["sort"] == "downloads"
    assert captured["params"]["search"] == "qwen"


async def test_invalid_sort_falls_back_to_lastModified():
    captured = {}
    async def _h(url, *, params, headers):
        captured["params"] = params
        return _mock_response([])
    with patch("dlw.ai.tools._httpx") as h:
        h.AsyncClient.return_value = _patched_client(_h)
        await READONLY_TOOLS["search_huggingface_models"].run(
            None, _principal(), query="x", sort="evil")
    assert captured["params"]["sort"] == "lastModified"


async def test_limit_clamped():
    captured = {}
    async def _h(url, *, params, headers):
        captured["params"] = params
        return _mock_response([])
    with patch("dlw.ai.tools._httpx") as h:
        h.AsyncClient.return_value = _patched_client(_h)
        await READONLY_TOOLS["search_huggingface_models"].run(
            None, _principal(), query="x", limit=999)
    assert captured["params"]["limit"] == 20


async def test_http_error_returns_error_dict():
    with patch("dlw.ai.tools._httpx") as h:
        h.AsyncClient.return_value = _patched_client(_mock_response([], status_code=503))
        out = await READONLY_TOOLS["search_huggingface_models"].run(
            None, _principal(), query="x")
    assert "error" in out
    assert "503" in out["error"]


async def test_network_exception_returns_error_dict():
    async def _h(url, *, params, headers):
        raise httpx.ConnectError("unreachable")
    with patch("dlw.ai.tools._httpx") as h:
        h.AsyncClient.return_value = _patched_client(_h)
        out = await READONLY_TOOLS["search_huggingface_models"].run(
            None, _principal(), query="x")
    assert "error" in out
    assert "request_failed" in out["error"]


async def test_empty_list_returns_empty_results():
    with patch("dlw.ai.tools._httpx") as h:
        h.AsyncClient.return_value = _patched_client(_mock_response([]))
        out = await READONLY_TOOLS["search_huggingface_models"].run(
            None, _principal(), query="no-match-xyz")
    assert out["total"] == 0
    assert out["results"] == []


async def test_hf_token_sent_when_configured(monkeypatch):
    from dlw.config import get_settings
    monkeypatch.setenv("DLW_HF_TOKEN", "hf_fake_token")
    get_settings.cache_clear()
    captured = {}
    async def _h(url, *, params, headers):
        captured["headers"] = headers
        return _mock_response([])
    with patch("dlw.ai.tools._httpx") as h:
        h.AsyncClient.return_value = _patched_client(_h)
        await READONLY_TOOLS["search_huggingface_models"].run(
            None, _principal(), query="x")
    assert captured["headers"].get("Authorization") == "Bearer hf_fake_token"
    get_settings.cache_clear()
