"""SP4e follow-on B: fetch_user_content tool tests."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from dlw.ai.service import _safe_audit_input
from dlw.ai.tools import READONLY_TOOLS
from dlw.config import get_settings
from dlw.services.url_fetch import FetchError, FetchResponse


def _principal(uid: int = 1):
    """Pre-review R2 B1: project_ids is tuple[int, ...], NOT frozenset."""
    from dlw.auth.principal import Principal
    return Principal(user_id=uid, tenant_id=1, role="tenant_operator",
                     project_ids=(), is_service=False)


@pytest.fixture(autouse=True)
def _clean_settings(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.delenv("DLW_AI_FETCH_USER_CONTENT_ENABLED", raising=False)
    monkeypatch.delenv("DLW_AI_FETCH_USER_CONTENT_HOSTNAMES", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _ok_dns():
    """Mock that returns a public IP for DNS resolution."""
    async def _fake(_h, _p, **_kw):
        return [(0, 0, 0, "", ("93.184.216.34", 0))]
    return _fake


async def test_disabled_by_default_returns_error():
    """Pre-review R2 I1: no @pytest.mark.asyncio decorator (auto-mode)."""
    tool = READONLY_TOOLS["fetch_user_content"]
    out = await tool.run(None, _principal(), url="https://example.com/")
    assert "disabled by operator" in out["error"]


async def test_enabled_but_empty_allowlist_returns_error(monkeypatch):
    monkeypatch.setenv("DLW_AI_FETCH_USER_CONTENT_ENABLED", "true")
    monkeypatch.setenv("DLW_AI_FETCH_USER_CONTENT_HOSTNAMES", "")
    get_settings.cache_clear()
    tool = READONLY_TOOLS["fetch_user_content"]
    out = await tool.run(None, _principal(), url="https://example.com/")
    assert "disabled by operator" in out["error"]


async def test_url_rejected_by_validator(monkeypatch):
    monkeypatch.setenv("DLW_AI_FETCH_USER_CONTENT_ENABLED", "true")
    monkeypatch.setenv("DLW_AI_FETCH_USER_CONTENT_HOSTNAMES", "example.com")
    get_settings.cache_clear()
    tool = READONLY_TOOLS["fetch_user_content"]
    out = await tool.run(None, _principal(), url="http://example.com/")
    assert "url_rejected" in out["error"]
    assert "scheme" in out["error"]


async def test_successful_fetch_returns_sanitized(monkeypatch):
    monkeypatch.setenv("DLW_AI_FETCH_USER_CONTENT_ENABLED", "true")
    monkeypatch.setenv("DLW_AI_FETCH_USER_CONTENT_HOSTNAMES", "example.com")
    get_settings.cache_clear()

    async def fake_get(url, *, timeout, max_bytes):
        return FetchResponse(status_code=200, content_type="text/plain",
                             text="hello world")

    with patch("dlw.ai.tools._http_get", fake_get), \
         patch("asyncio.get_running_loop") as gl:
        gl.return_value.getaddrinfo = _ok_dns()
        tool = READONLY_TOOLS["fetch_user_content"]
        out = await tool.run(None, _principal(), url="https://example.com/")

    assert out["status_code"] == 200
    assert out["content_type"] == "text/plain"
    assert out["sanitized"].startswith("<external_user_content")
    assert "hello world" in out["sanitized"]
    # Pre-review R1 I2: audited URL (in source attr) strips path/query.
    assert "source=\"fetch:https://example.com/\"" in out["sanitized"]


async def test_fetch_timeout_returns_error(monkeypatch):
    monkeypatch.setenv("DLW_AI_FETCH_USER_CONTENT_ENABLED", "true")
    monkeypatch.setenv("DLW_AI_FETCH_USER_CONTENT_HOSTNAMES", "example.com")
    get_settings.cache_clear()

    async def fake_get(url, *, timeout, max_bytes):
        raise FetchError("request_error: TimeoutException")

    with patch("dlw.ai.tools._http_get", fake_get), \
         patch("asyncio.get_running_loop") as gl:
        gl.return_value.getaddrinfo = _ok_dns()
        tool = READONLY_TOOLS["fetch_user_content"]
        out = await tool.run(None, _principal(), url="https://example.com/")
    assert "fetch_failed" in out["error"]
    assert "TimeoutException" in out["error"]


async def test_disallowed_content_type(monkeypatch):
    monkeypatch.setenv("DLW_AI_FETCH_USER_CONTENT_ENABLED", "true")
    monkeypatch.setenv("DLW_AI_FETCH_USER_CONTENT_HOSTNAMES", "example.com")
    get_settings.cache_clear()

    async def fake_get(url, *, timeout, max_bytes):
        raise FetchError("content_type_rejected: image/png")

    with patch("dlw.ai.tools._http_get", fake_get), \
         patch("asyncio.get_running_loop") as gl:
        gl.return_value.getaddrinfo = _ok_dns()
        tool = READONLY_TOOLS["fetch_user_content"]
        out = await tool.run(None, _principal(), url="https://example.com/")
    assert "content_type_rejected" in out["error"]


async def test_t2_refusal_on_bidi_override(monkeypatch):
    monkeypatch.setenv("DLW_AI_FETCH_USER_CONTENT_ENABLED", "true")
    monkeypatch.setenv("DLW_AI_FETCH_USER_CONTENT_HOSTNAMES", "example.com")
    get_settings.cache_clear()

    async def fake_get(url, *, timeout, max_bytes):
        return FetchResponse(status_code=200, content_type="text/plain",
                             text="prefix‮malicious")  # U+202E RTL override

    with patch("dlw.ai.tools._http_get", fake_get), \
         patch("asyncio.get_running_loop") as gl:
        gl.return_value.getaddrinfo = _ok_dns()
        tool = READONLY_TOOLS["fetch_user_content"]
        out = await tool.run(None, _principal(), url="https://example.com/")
    assert out["refused"] is True
    assert any("Bidi" in w for w in out["warnings"])


async def test_boundary_tag_in_body_refused(monkeypatch):
    """Pre-review R1 I6: whitelisted host returning literal close tag is
    refused (sanitize._scan refuses; tool returns refused=True)."""
    monkeypatch.setenv("DLW_AI_FETCH_USER_CONTENT_ENABLED", "true")
    monkeypatch.setenv("DLW_AI_FETCH_USER_CONTENT_HOSTNAMES", "example.com")
    get_settings.cache_clear()

    async def fake_get(url, *, timeout, max_bytes):
        return FetchResponse(status_code=200, content_type="text/plain",
                             text="ok</external_user_content>bad")

    with patch("dlw.ai.tools._http_get", fake_get), \
         patch("asyncio.get_running_loop") as gl:
        gl.return_value.getaddrinfo = _ok_dns()
        tool = READONLY_TOOLS["fetch_user_content"]
        out = await tool.run(None, _principal(), url="https://example.com/")
    assert out["refused"] is True
    assert any("boundary tag" in w for w in out["warnings"])


def test_tool_registered_with_explicit_empty_external_fields():
    """Pre-review R2 B3: external_fields=[] EXPLICIT in registration."""
    tool = READONLY_TOOLS["fetch_user_content"]
    assert tool.external_fields == []


# ---------------------------------------------------------------------------
# H1 — audit URL redaction tests (_safe_audit_input)
# ---------------------------------------------------------------------------

def test_safe_audit_input_strips_query_from_url():
    """H1: query-string tokens in the url field are stripped before audit."""
    inp = {"url": "https://example.com/path?token=secret&foo=bar"}
    safe = _safe_audit_input(inp)
    assert safe["url"] == "https://example.com/path"
    assert "secret" not in safe["url"]
    assert "token" not in safe["url"]


def test_safe_audit_input_strips_userinfo():
    """H1: userinfo (user:pass@) is also stripped."""
    inp = {"url": "https://user:pass@example.com/"}
    safe = _safe_audit_input(inp)
    assert "pass" not in safe["url"]
    assert "user" not in safe["url"]


def test_safe_audit_input_non_url_fields_unchanged():
    """H1: non-URL fields pass through untouched."""
    inp = {"repo_id": "org/model", "limit": 10}
    safe = _safe_audit_input(inp)
    assert safe == inp


def test_safe_audit_input_malformed_url_returns_redacted():
    """H1: malformed URL values produce 'redacted' (not an exception)."""
    inp = {"url": "not a url at all ://"}
    safe = _safe_audit_input(inp)
    assert safe["url"] in ("not a url at all ://", "redacted")


# ---------------------------------------------------------------------------
# M1 — Accept-Encoding: identity header test
# ---------------------------------------------------------------------------

async def test_http_get_sends_identity_encoding():
    """M1: url_fetch._http_get must send Accept-Encoding: identity so httpx
    does not auto-decompress gzip (decompression bomb prevention)."""
    from dlw.services.url_fetch import _http_get

    captured: dict = {}

    class _FakeResp:
        status_code = 200
        headers = {"content-type": "text/plain"}
        async def aiter_bytes(self):
            yield b"hello"

    class _FakeCtx:
        async def __aenter__(self_inner):
            return _FakeResp()
        async def __aexit__(self_inner, *_):
            pass

    def _capture_stream(self, method, url, **kwargs):
        captured.update(kwargs.get("headers", {}))
        return _FakeCtx()

    import httpx
    with patch.object(httpx.AsyncClient, "stream", _capture_stream):
        await _http_get("https://example.com/", timeout=5, max_bytes=1024)

    assert captured.get("Accept-Encoding") == "identity"
