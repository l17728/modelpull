"""SP4e follow-on: Brave Search API client (search_brave) tests."""
from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from dlw.services.web_search import (
    BRAVE_ENDPOINT,
    WebSearchError,
    WebSearchResult,
    search_brave,
)


class _FakeResponse:
    def __init__(self, status_code: int, payload=None, raise_json=False):
        self.status_code = status_code
        self._payload = payload or {}
        self._raise_json = raise_json
        self.headers = {"content-type": "application/json"}

    def json(self):
        if self._raise_json:
            raise ValueError("bad json")
        return self._payload


async def test_happy_path_returns_results():
    payload = {"web": {"results": [
        {"title": "T1", "url": "https://a.example/x?u=1", "description": "D1"},
        {"title": "T2", "url": "https://b.example/y", "description": "D2"},
    ]}}

    async def fake_get(self, url, *, params=None, headers=None):
        assert url == BRAVE_ENDPOINT
        assert params["q"] == "hello"
        assert params["count"] == 2
        assert headers["X-Subscription-Token"] == "k"
        assert headers["Accept-Encoding"] == "identity"
        return _FakeResponse(200, payload)

    with patch.object(httpx.AsyncClient, "get", fake_get):
        out = await search_brave("hello", api_key="k", count=2, timeout=5)
    assert len(out) == 2
    assert isinstance(out[0], WebSearchResult)
    assert out[0].title == "T1"
    assert out[0].url == "https://a.example/x?u=1"
    assert out[0].description == "D1"


async def test_401_raises_invalid_api_key():
    async def fake_get(self, url, *, params=None, headers=None):
        return _FakeResponse(401)

    with patch.object(httpx.AsyncClient, "get", fake_get), \
         pytest.raises(WebSearchError, match="invalid_api_key"):
        await search_brave("q", api_key="bad", count=5, timeout=5)


async def test_429_raises_rate_limited():
    async def fake_get(self, url, *, params=None, headers=None):
        return _FakeResponse(429)

    with patch.object(httpx.AsyncClient, "get", fake_get), \
         pytest.raises(WebSearchError, match="rate_limited"):
        await search_brave("q", api_key="k", count=5, timeout=5)


async def test_500_raises_http_status():
    async def fake_get(self, url, *, params=None, headers=None):
        return _FakeResponse(503)

    with patch.object(httpx.AsyncClient, "get", fake_get), \
         pytest.raises(WebSearchError, match="http_503"):
        await search_brave("q", api_key="k", count=5, timeout=5)


async def test_network_error_translated_to_request_error():
    async def fake_get(self, url, *, params=None, headers=None):
        raise httpx.ConnectError("dns fail")

    with patch.object(httpx.AsyncClient, "get", fake_get), \
         pytest.raises(WebSearchError, match="request_error: ConnectError"):
        await search_brave("q", api_key="k", count=5, timeout=5)


async def test_bad_json_translated_to_invalid_json():
    async def fake_get(self, url, *, params=None, headers=None):
        return _FakeResponse(200, raise_json=True)

    with patch.object(httpx.AsyncClient, "get", fake_get), \
         pytest.raises(WebSearchError, match="invalid_json"):
        await search_brave("q", api_key="k", count=5, timeout=5)


async def test_count_caps_result_list():
    payload = {"web": {"results": [
        {"title": f"T{i}", "url": f"https://e{i}.example/",
         "description": f"D{i}"} for i in range(10)
    ]}}

    async def fake_get(self, url, *, params=None, headers=None):
        return _FakeResponse(200, payload)

    with patch.object(httpx.AsyncClient, "get", fake_get):
        out = await search_brave("q", api_key="k", count=3, timeout=5)
    assert len(out) == 3
    assert out[0].title == "T0"
    assert out[2].title == "T2"
