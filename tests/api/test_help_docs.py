"""Tests for the docs-list / docs-fetch endpoints."""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from dlw.config import get_settings


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setenv("DLW_AUTH_DEV_MODE", "true")
    monkeypatch.setenv("DLW_SYSTEM_JWT_SECRET", "qa-secret-32-bytes-pad-pad!!!")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def client(ephemeral_ca):
    from tests.conftest import make_app_with_state
    app = make_app_with_state(ephemeral_ca, enrollment_token="e")
    async with AsyncClient(transport=ASGITransport(app=app),
                            base_url="http://test") as c:
        yield c


async def test_list_docs_returns_all_curated_slugs(client):
    r = await client.get("/api/v1/help/docs")
    assert r.status_code == 200
    body = r.json()
    slugs = {item["slug"] for item in body["items"]}
    # The curated set we expose (4 v2.0 + 2 v2.1)
    assert slugs == {
        "ai-troubleshooting", "local-auth", "sla-slo", "qa-test-plan",
        "v21-production-deployment", "post-mortem-template"}
    for item in body["items"]:
        assert item["title_en"]
        assert item["title_zh"]


async def test_get_doc_returns_markdown_content(client):
    r = await client.get("/api/v1/help/docs/ai-troubleshooting")
    assert r.status_code == 200
    content = r.json()["content"]
    # The runbook should mention its purpose
    assert "AI Copilot" in content or "AI 助手" in content


async def test_get_doc_unknown_slug_returns_404(client):
    r = await client.get("/api/v1/help/docs/totally-not-a-real-doc")
    assert r.status_code == 404


async def test_get_doc_path_traversal_blocked(client):
    """The allowlist is a dict lookup, so path traversal is structurally
    impossible — the slug never reaches the filesystem unless it matches
    a key. Sanity check the obvious attacks return 404."""
    for evil in ["../etc/passwd", "..%2Fetc%2Fpasswd",
                  "ai-troubleshooting/../../README"]:
        r = await client.get(f"/api/v1/help/docs/{evil}")
        assert r.status_code in (404, 422), \
            f"{evil!r} should not be served (got {r.status_code})"


async def test_qa_test_plan_doc_loadable(client):
    """Specifically verify the QA doc — it's the one we want testers to
    open from the UI."""
    r = await client.get("/api/v1/help/docs/qa-test-plan")
    assert r.status_code == 200
    content = r.json()["content"]
    assert "QA 测试清单" in content or "QA test plan" in content
