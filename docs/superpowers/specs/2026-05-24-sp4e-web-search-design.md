# SP4e follow-on — `web_search` AI tool (Brave Search API, free tier)

## Problem

SP4e-B deferred `web_search` pending an operator API-provider decision. The decision is now made: **Brave Search API, free tier** (2000 queries/month, no cost, requires free API key). This PR ships `web_search` reusing the existing `url_guard` + `url_fetch` module layout and the SP4e-A choke-point pattern.

## §0 Design

### Admin gate (two settings)

`src/dlw/config.py` additions:

```python
ai_web_search_enabled: bool = Field(default=False)
ai_web_search_api_key: str = Field(default="")        # Brave Search API key
ai_web_search_result_count: int = Field(default=5, ge=1, le=10)
ai_web_search_timeout_seconds: float = Field(default=10.0, ge=1.0, le=30.0)
```

Both `enabled=true` AND a non-empty `api_key` are required. Missing either returns
`{"error": "web_search disabled by operator"}`.

Env vars: `DLW_AI_WEB_SEARCH_ENABLED`, `DLW_AI_WEB_SEARCH_API_KEY`,
`DLW_AI_WEB_SEARCH_RESULT_COUNT`, `DLW_AI_WEB_SEARCH_TIMEOUT_SECONDS`.

### HTTP helper (`src/dlw/services/web_search.py`, new)

```python
BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

class WebSearchError(RuntimeError): ...

@dataclass
class WebSearchResult:
    title: str
    url: str
    description: str

async def search_brave(query: str, *, api_key: str,
                       count: int = 5, timeout: float = 10.0) -> list[WebSearchResult]:
    async with httpx.AsyncClient(
        follow_redirects=False, timeout=timeout, verify=True,
    ) as client:
        try:
            resp = await client.get(
                BRAVE_ENDPOINT,
                params={"q": query, "count": count},
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",   # M1 decompression guard
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

Key properties:
- `follow_redirects=False` (consistent with `url_fetch.py`)
- `Accept-Encoding: identity` (M1 decompression guard — same reasoning as `url_fetch`)
- No body-size cap needed (JSON response from a known API endpoint, typical < 50 KB)
- Treats 401 / 429 as distinct errors for operator observability
- Caps result list at `count` after parsing (defensive slice)

### Tool implementation (`src/dlw/ai/tools.py`)

```python
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
        desc_res = sanitize_t2(r.description, source=f"web-desc:{r.url}")
        items.append({
            "title": title_res.text,
            "url": _audit_safe_url(r.url),
            "description": desc_res.text,
            "refused": title_res.refused or desc_res.refused,
        })
    return {"query": query, "results": items}
```

`READONLY_TOOLS` registration:

```python
"web_search": Tool(
    "web_search",
    "Search the web for current information. Returns up to 5 sanitized results "
    "(T2 trust). Operator-gated: requires DLW_AI_WEB_SEARCH_ENABLED=true and "
    "DLW_AI_WEB_SEARCH_API_KEY set to a Brave Search API key (free at "
    "https://brave.com/search/api/).",
    {"type": "object", "required": ["query"], "properties": {
        "query": {"type": "string", "description": "The search query."}}},
    _web_search,
    external_fields=[]),  # pre-wraps inline; choke point no-op
```

### Stub runner trigger

Add `"web_search"` keyword to the stub runner so `"web_search <term>"` user
messages exercise the tool path in CI (zero-secret, deterministic).

## §1 Threat model

| Threat | Defense |
|---|---|
| Arbitrary egress to internal services | Fixed endpoint (`api.search.brave.com`) — no operator-supplied URL, no URL guard needed |
| Content injection from result titles/descriptions | `sanitize_t2` wraps each title + description; refused items flagged |
| API key exfiltration via audit log | `_audit_safe_url` applied to result URLs (strips query); API key is in request header, never returned or logged |
| Rate-limit amplification | Operator-configurable `result_count` (1–10); single API call per tool invocation |
| DoS via large response | JSON is small (< 50 KB typical); httpx timeout gate (`10s` default) |
| Redirect escape | `follow_redirects=False` |
| Decompression bomb | `Accept-Encoding: identity` |

Not defended: Brave API serving manipulated results (standard trusted-third-party
assumption; same class as `hf_api_metadata`).

## §2 Tests

`tests/ai/test_web_search.py` (new):
- Disabled flag → `{"error": "web_search disabled by operator"}`.
- Enabled, empty key → `{"error": "...no API key..."}`.
- `search_brave` mock returns results → tool returns `{"query": ..., "results": [...]}` with `title`/`url`/`description` all wrapped (`sanitize_t2` applied).
- `search_brave` raises `WebSearchError("rate_limited")` → `{"error": "search_failed: rate_limited"}`.
- `search_brave` returns item with Bidi-override in description → `refused=True` on that item.
- `search_brave` returns item with boundary tag in title → `refused=True`.
- `_audit_safe_url` applied to result URLs (query strings stripped).
- Tool registered in `READONLY_TOOLS` with `external_fields=[]`.

`tests/services/test_brave_search.py` (new):
- `search_brave` happy path (mock httpx) → returns `list[WebSearchResult]` with correct fields.
- `search_brave` 401 response → raises `WebSearchError("invalid_api_key")`.
- `search_brave` 429 response → raises `WebSearchError("rate_limited")`.
- `search_brave` network error → raises `WebSearchError("request_error: ...")`.
- `search_brave` `Accept-Encoding: identity` header is sent.
- `search_brave` caps result count at `count` parameter.

## §3 Files

- **Modify** `src/dlw/config.py` — 4 new `ai_web_search_*` settings.
- **Create** `src/dlw/services/web_search.py` — `WebSearchError`, `WebSearchResult`, `search_brave`.
- **Modify** `src/dlw/ai/tools.py` — add `_web_search`; register `web_search` in `READONLY_TOOLS`; import `search_brave`/`WebSearchError`.
- **Modify** `src/dlw/ai/runner.py` (stub) — add `web_search` trigger keyword.
- **Create** `tests/ai/test_web_search.py` — 8 tool-level tests.
- **Create** `tests/services/test_brave_search.py` — 6 service-level tests.

Zero migration / openapi / frontend change.

## §4 Notes

- Brave Search API free tier: 2000 queries/month. Sign up at https://brave.com/search/api/.
  Env var: `DLW_AI_WEB_SEARCH_API_KEY=<your-key>`.
- `web_search` and `fetch_user_content` share `_audit_safe_url` (tools.py) and the
  `sanitize_t2` + T2 trust model. Both are operator-gated disabled-by-default.
- `web_search` result URLs are returned via `_audit_safe_url` (strip query). This means
  the LLM sees clean URLs without tracking/session tokens, consistent with privacy-by-default.
- Lint gate: `pytest -q` + `lint_invariants --strict`. No new invariant added.
