"""search_modelscope_models tool tests."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dlw.ai.tools import READONLY_TOOLS


def _principal(uid: int = 1):
    from dlw.auth.principal import Principal
    return Principal(user_id=uid, tenant_id=1, role="tenant_operator",
                     project_ids=(), is_service=False)


def _mock_response(data: dict, *, status_code: int = 200):
    """Build a minimal httpx.Response mock."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json = MagicMock(return_value=data)
    return resp


def _modelscope_payload(models: list[dict]) -> dict:
    return {"Code": 200, "Data": {"Models": models, "Total": len(models)}}


_SAMPLE_MODELS = [
    {"Name": "Qwen-7B-Chat", "Path": "qwen",
     "ChineseName": "通义千问7B对话版",
     "Downloads": 12345, "Tags": ["NLP", "Chat"]},
    {"Name": "CLIP-ViT-B-32", "Path": "AI-ModelScope",
     "ChineseName": "", "Downloads": 5000, "Tags": ["CV", "CLIP"]},
]


async def _fake_get_ok(url, *, params, headers):
    return _mock_response(_modelscope_payload(_SAMPLE_MODELS))


async def test_tool_registered():
    assert "search_modelscope_models" in READONLY_TOOLS
    t = READONLY_TOOLS["search_modelscope_models"]
    assert t.name == "search_modelscope_models"
    assert "modelscope" in t.description.lower()


async def test_happy_path_returns_results():
    with patch("dlw.ai.tools._httpx") as mock_httpx:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(
            return_value=_mock_response(_modelscope_payload(_SAMPLE_MODELS)))
        mock_httpx.AsyncClient.return_value = mock_client

        tool = READONLY_TOOLS["search_modelscope_models"]
        out = await tool.run(None, _principal(), query="qwen chat")

    assert out["query"] == "qwen chat"
    assert out["total"] == 2
    assert len(out["results"]) == 2


async def test_result_contains_expected_fields():
    with patch("dlw.ai.tools._httpx") as mock_httpx:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(
            return_value=_mock_response(_modelscope_payload(_SAMPLE_MODELS)))
        mock_httpx.AsyncClient.return_value = mock_client

        tool = READONLY_TOOLS["search_modelscope_models"]
        out = await tool.run(None, _principal(), query="qwen")

    first = out["results"][0]
    assert "id" in first
    assert "name" in first
    assert "description" in first
    assert "downloads" in first
    assert "tags" in first
    assert "url" in first
    assert first["downloads"] == 12345
    assert "modelscope.cn" in first["url"]


async def test_description_is_t2_sanitized():
    with patch("dlw.ai.tools._httpx") as mock_httpx:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(
            return_value=_mock_response(_modelscope_payload(_SAMPLE_MODELS)))
        mock_httpx.AsyncClient.return_value = mock_client

        tool = READONLY_TOOLS["search_modelscope_models"]
        out = await tool.run(None, _principal(), query="qwen")

    # T2 sanitization wraps content in <external_user_content ...>
    first = out["results"][0]
    assert first["description"].startswith("<external_user_content")


async def test_http_error_returns_error_dict():
    with patch("dlw.ai.tools._httpx") as mock_httpx:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(
            return_value=_mock_response({}, status_code=503))
        mock_httpx.AsyncClient.return_value = mock_client

        tool = READONLY_TOOLS["search_modelscope_models"]
        out = await tool.run(None, _principal(), query="anything")

    assert "error" in out
    assert "503" in out["error"]


async def test_network_exception_returns_error_dict():
    import httpx

    with patch("dlw.ai.tools._httpx") as mock_httpx:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(
            side_effect=httpx.ConnectError("unreachable"))
        mock_httpx.AsyncClient.return_value = mock_client

        tool = READONLY_TOOLS["search_modelscope_models"]
        out = await tool.run(None, _principal(), query="anything")

    assert "error" in out
    assert "request_failed" in out["error"]


async def test_invalid_json_returns_error():
    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(side_effect=ValueError("bad json"))

    with patch("dlw.ai.tools._httpx") as mock_httpx:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=resp)
        mock_httpx.AsyncClient.return_value = mock_client

        tool = READONLY_TOOLS["search_modelscope_models"]
        out = await tool.run(None, _principal(), query="q")

    assert out["error"] == "invalid_json"


async def test_limit_clamped_to_20():
    captured: dict = {}

    async def _fake_get(url, *, params, headers):
        captured["params"] = params
        return _mock_response(_modelscope_payload([]))

    with patch("dlw.ai.tools._httpx") as mock_httpx:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=_fake_get)
        mock_httpx.AsyncClient.return_value = mock_client

        tool = READONLY_TOOLS["search_modelscope_models"]
        await tool.run(None, _principal(), query="q", limit=999)

    assert captured["params"]["PageSize"] == 20


async def test_limit_minimum_clamped_to_1():
    captured: dict = {}

    async def _fake_get(url, *, params, headers):
        captured["params"] = params
        return _mock_response(_modelscope_payload([]))

    with patch("dlw.ai.tools._httpx") as mock_httpx:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=_fake_get)
        mock_httpx.AsyncClient.return_value = mock_client

        tool = READONLY_TOOLS["search_modelscope_models"]
        await tool.run(None, _principal(), query="q", limit=0)

    assert captured["params"]["PageSize"] == 1


async def test_empty_models_list_returns_empty_results():
    with patch("dlw.ai.tools._httpx") as mock_httpx:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(
            return_value=_mock_response(_modelscope_payload([])))
        mock_httpx.AsyncClient.return_value = mock_client

        tool = READONLY_TOOLS["search_modelscope_models"]
        out = await tool.run(None, _principal(), query="no-match-xyz")

    assert out["total"] == 0
    assert out["results"] == []


async def test_bidi_in_description_sanitize_clears_text():
    """sanitize_t2 strips bidi-overriding characters and returns empty text with refused=True.
    The tool should still return a result entry (not crash); the description will be empty."""
    models = [{"Name": "m", "Path": "p",
               "ChineseName": "evil‮flip",
               "Downloads": 1, "Tags": []}]

    with patch("dlw.ai.tools._httpx") as mock_httpx:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(
            return_value=_mock_response(_modelscope_payload(models)))
        mock_httpx.AsyncClient.return_value = mock_client

        tool = READONLY_TOOLS["search_modelscope_models"]
        out = await tool.run(None, _principal(), query="evil")

    # sanitize_t2 returns empty string on refusal; result entry still present
    assert len(out["results"]) == 1
    assert out["results"][0]["description"] == ""


async def test_null_data_field_returns_empty():
    with patch("dlw.ai.tools._httpx") as mock_httpx:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(
            return_value=_mock_response({"Code": 200, "Data": None}))
        mock_httpx.AsyncClient.return_value = mock_client

        tool = READONLY_TOOLS["search_modelscope_models"]
        out = await tool.run(None, _principal(), query="x")

    assert out["results"] == []
    assert out["total"] == 0
