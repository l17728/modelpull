# SP4e follow-on — `web_search` AI Tool (Brave Search API) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: subagent-driven-development / executing-plans. Checkbox steps.

**Goal:** Ship `web_search` as an operator-gated AI Copilot tool backed by the Brave Search API free tier, reusing the SP4e-A choke-point pattern and SP4e-B `url_guard`/`url_fetch` layout.

**Spec:** `docs/superpowers/specs/2026-05-24-sp4e-web-search-design.md`

**Locked constraints:**
- Operator must set both `DLW_AI_WEB_SEARCH_ENABLED=true` AND `DLW_AI_WEB_SEARCH_API_KEY` — missing either returns `{"error": "web_search disabled by operator"}`.
- `follow_redirects=False` and `Accept-Encoding: identity` on the outgoing httpx request (consistent with `url_fetch.py`).
- Each result title and description is wrapped with `sanitize_t2`; refused items have `"refused": True` in the output.
- Result URLs pass through `_audit_safe_url` (query-string stripped) before being returned.
- `external_fields=[]` in `READONLY_TOOLS` registration — choke-point is a no-op because sanitization is done inline per result.
- Stub runner: add `"web_search"` / `"search the web"` / `"搜索"` trigger so CI exercises the tool path without a real API key.
- Zero migration, zero OpenAPI, zero frontend change.
- Lint gates: `uv run pytest -q` + `uv run python tools/lint_invariants.py --strict`.

---

## File Structure

- **Modify** `src/dlw/config.py` — 4 new `ai_web_search_*` fields.
- **Create** `src/dlw/services/web_search.py` — `WebSearchError`, `WebSearchResult` dataclass, `search_brave` async function.
- **Modify** `src/dlw/ai/tools.py` — `_web_search` tool function; register `"web_search"` in `READONLY_TOOLS`; import `search_brave`/`WebSearchError`.
- **Modify** `src/dlw/ai/runner.py` (stub) — add `web_search` / `"search the web"` / `"搜索"` trigger.
- **Create** `tests/ai/test_web_search.py` — 8 tool-level tests.
- **Create** `tests/services/test_brave_search.py` — 6 service-level tests.

---

## Milestone M1 — Config fields

### Task 1: add config fields

**Files:** modify `src/dlw/config.py`.

- [x] **Step 1 (failing test):** add test asserting `s.ai_web_search_enabled is False` (default off) and `s.ai_web_search_result_count == 5`.
- [x] **Step 2 (run):** confirm test fails with `AttributeError`.
- [x] **Step 3 (implement):** add to `Settings`:

  ```python
  ai_web_search_enabled: bool = Field(default=False)
  ai_web_search_api_key: str = Field(default="")
  ai_web_search_result_count: int = Field(default=5, ge=1, le=10)
  ai_web_search_timeout_seconds: float = Field(default=10.0, ge=1.0, le=30.0)
  ```

- [x] **Step 4 (run):** test passes.
- [x] **Step 5 (commit):** `git commit -m "feat(config): add ai_web_search_* settings"`

---

## Milestone M2 — `web_search.py` service module

### Task 2: `search_brave` + unit tests

**Files:** create `src/dlw/services/web_search.py`, create `tests/services/test_brave_search.py`.

- [x] **Step 1 (failing tests):** create `tests/services/test_brave_search.py`:

  ```python
  """Unit tests for search_brave (Brave Search API client)."""
  import pytest
  import httpx
  from unittest.mock import AsyncMock, patch, MagicMock

  from dlw.services.web_search import WebSearchError, WebSearchResult, search_brave

  @pytest.mark.anyio
  async def test_happy_path(respx_mock):
      # mock httpx GET → 200 with two results
      # assert returns list[WebSearchResult] with correct fields

  @pytest.mark.anyio
  async def test_401_raises_invalid_api_key(respx_mock): ...

  @pytest.mark.anyio
  async def test_429_raises_rate_limited(respx_mock): ...

  @pytest.mark.anyio
  async def test_network_error_raises_request_error(respx_mock): ...

  @pytest.mark.anyio
  async def test_accept_encoding_identity_header(respx_mock): ...

  @pytest.mark.anyio
  async def test_caps_result_count(respx_mock): ...
  ```

- [x] **Step 2 (run):** confirm `ImportError` (module not yet created).
- [x] **Step 3 (implement)** `src/dlw/services/web_search.py`:

  ```python
  from __future__ import annotations
  from dataclasses import dataclass
  import httpx

  BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

  class WebSearchError(RuntimeError): ...

  @dataclass
  class WebSearchResult:
      title: str
      url: str
      description: str

  async def search_brave(
      query: str, *, api_key: str, count: int = 5, timeout: float = 10.0,
  ) -> list[WebSearchResult]:
      async with httpx.AsyncClient(
          follow_redirects=False, timeout=timeout, verify=True,
      ) as client:
          try:
              resp = await client.get(
                  BRAVE_ENDPOINT,
                  params={"q": query, "count": count},
                  headers={
                      "Accept": "application/json",
                      "Accept-Encoding": "identity",
                      "X-Subscription-Token": api_key,
                  },
              )
          except httpx.RequestError as e:
              raise WebSearchError(f"request_error: {type(e).__name__}") from e
          if resp.status_code == 401:
              raise WebSearchError("invalid_api_key")
          if resp.status_code == 429:
              raise WebSearchError("rate_limited")
          if resp.status_code != 200:
              raise WebSearchError(f"http_{resp.status_code}")
          try:
              data = resp.json()
          except Exception as e:
              raise WebSearchError("invalid_json") from e
          out = []
          for item in (data.get("web", {}).get("results") or []):
              out.append(WebSearchResult(
                  title=str(item.get("title") or ""),
                  url=str(item.get("url") or ""),
                  description=str(item.get("description") or ""),
              ))
          return out[:count]
  ```

- [x] **Step 4 (run):** 6 tests pass.
- [x] **Step 5 (commit):** `git commit -m "feat(web-search): add search_brave service (Brave Search API)"`

---

## Milestone M3 — `_web_search` tool + registration

### Task 3: tool function + READONLY_TOOLS registration

**Files:** modify `src/dlw/ai/tools.py`, create `tests/ai/test_web_search.py`.

- [x] **Step 1 (failing tests):** create `tests/ai/test_web_search.py`:

  ```python
  """Unit tests for _web_search AI Copilot tool."""
  import pytest
  from unittest.mock import AsyncMock, patch

  from dlw.ai.tools import READONLY_TOOLS

  @pytest.mark.anyio
  async def test_disabled_flag():
      # settings.ai_web_search_enabled=False → {"error": "web_search disabled by operator"}

  @pytest.mark.anyio
  async def test_enabled_no_key():
      # enabled=True, api_key="" → {"error": "...no API key..."}

  @pytest.mark.anyio
  async def test_happy_path_returns_results():
      # mock search_brave → tool returns {"query": q, "results": [...]}
      # title/url/description present, sanitize_t2 applied

  @pytest.mark.anyio
  async def test_rate_limited_returns_error():
      # WebSearchError("rate_limited") → {"error": "search_failed: rate_limited"}

  @pytest.mark.anyio
  async def test_refused_item_bidi_override():
      # Bidi-override in description → item has refused=True

  @pytest.mark.anyio
  async def test_refused_item_boundary_tag_in_title():
      # boundary tag in title → item has refused=True

  @pytest.mark.anyio
  async def test_audit_safe_url_applied():
      # result URL with query string → query stripped before return

  def test_registered_in_readonly_tools():
      assert "web_search" in READONLY_TOOLS
      assert READONLY_TOOLS["web_search"].external_fields == []
  ```

- [x] **Step 2 (run):** fail with `KeyError` on `"web_search"` (not yet registered).
- [x] **Step 3 (implement)** — add to `tools.py`:

  ```python
  from dlw.services.web_search import WebSearchError, search_brave

  async def _web_search(session, principal, *, query: str) -> dict:
      s = get_settings()
      if not s.ai_web_search_enabled:
          return {"error": "web_search disabled by operator"}
      if not s.ai_web_search_api_key:
          return {"error": "web_search disabled by operator (no API key)"}
      try:
          results = await search_brave(
              query, api_key=s.ai_web_search_api_key,
              count=s.ai_web_search_result_count,
              timeout=s.ai_web_search_timeout_seconds)
      except WebSearchError as e:
          return {"error": f"search_failed: {e}"}
      items = []
      for r in results:
          title_res = sanitize_t2(r.title, source=f"web-title:{r.url}")
          desc_res  = sanitize_t2(r.description, source=f"web-desc:{r.url}")
          items.append({
              "title": title_res.text,
              "url":   _audit_safe_url(r.url),
              "description": desc_res.text,
              "refused": title_res.refused or desc_res.refused,
          })
      return {"query": query, "results": items}
  ```

  Register in `READONLY_TOOLS`:

  ```python
  "web_search": Tool(
      "web_search",
      "Search the web for current information. Returns up to 5 sanitized results "
      "(T2 trust). Operator-gated: requires DLW_AI_WEB_SEARCH_ENABLED=true and "
      "DLW_AI_WEB_SEARCH_API_KEY set to a Brave Search API key.",
      {"type": "object", "required": ["query"], "properties": {
          "query": {"type": "string", "description": "The search query."}}},
      _web_search,
      external_fields=[]),
  ```

- [x] **Step 4 (run):** 8 tests pass.
- [x] **Step 5 (commit):** `git commit -m "feat(ai-tools): add web_search tool (Brave Search, operator-gated)"`

---

## Milestone M4 — Stub runner trigger + CI gate

### Task 4: add stub trigger keyword + final gate

**Files:** modify `src/dlw/ai/runner.py`.

- [x] **Step 1 (implement):** in `StubAgentRunner.run`, add a branch for `"web_search"` / `"search the web"` / `"搜索"` before the task-keyword branch, stripping the trigger prefix to form the bare query, then:

  ```python
  yield AgentEvent("tool_call", {"id": "call_ws", "tool": "web_search",
                                  "input": {"query": q}, "requires_confirmation": False})
  result = await call_tool("web_search", {"query": q})
  yield AgentEvent("tool_result", {"id": "call_ws", "ok": "error" not in result,
                                    "output": result})
  n = len(result.get("results", []))
  yield AgentEvent("assistant.message_delta",
                    {"text": f"Found {n} result(s) for '{q}'."})
  return
  ```

- [x] **Step 2 (run full suite):**

  ```
  uv run pytest -q
  ```

  Expected: all tests pass (including stub runner exercising the tool with the mocked `call_tool`).

- [x] **Step 3 (lint):**

  ```
  uv run python tools/lint_invariants.py --strict
  ```

  Expected: exit 0.

- [x] **Step 4 (commit):** `git commit -m "feat(stub-runner): add web_search trigger for CI coverage"`

---

## Test Plan

- [x] `tests/services/test_brave_search.py` — 6 tests: happy path, 401, 429, network error, `Accept-Encoding` header, count cap.
- [x] `tests/ai/test_web_search.py` — 8 tests: disabled, no key, happy path, rate-limited error, Bidi-override refused, boundary-tag refused, `_audit_safe_url` applied to URLs, registration check.
- [x] `uv run pytest -q` — full suite green.
- [x] `uv run python tools/lint_invariants.py --strict` — exit 0.
- [x] Zero migration, zero OpenAPI spec change, zero frontend change.
