# SP4e follow-on — `fetch_user_content` AI tool (arbitrary egress + admin whitelist)

## Problem

SP4e (PR #34) added the `hf_api_metadata` and `hf_model_card` AI tools — both scoped to a fixed HF endpoint, so the egress surface is narrow. The named follow-on `fetch_user_content` opens **arbitrary egress**: the LLM (driven by a user prompt) can ask to fetch any URL the user mentions, and the controller fetches it on the user's behalf, sanitizes the body, and returns it to the LLM context.

This is the **highest-risk SP4e follow-on** because it introduces SSRF surface, third-party egress, and the standard inv-19 external-content trust problem at scale. Honest scope: this PR ships `fetch_user_content` with a strict admin gate + hostname whitelist; `web_search` is **not** in scope (needs a search-API choice — Tavily/Brave/SerpAPI/etc. — that the operator must make; deferred as a named follow-on of this follow-on).

## §0 Design — disabled-by-default, hostname-whitelist, T2 sanitize

### Admin gate (two settings, both must be set for the tool to function)

`src/dlw/config.py` additions:

```python
# SP4e follow-on B: fetch_user_content — arbitrary egress AI tool.
# DEFAULT OFF: opt-in by operator. Empty hostname list = effective disable
# even if enabled (no allowed host → every URL rejected).
fetch_user_content_enabled: bool = Field(default=False)
fetch_user_content_hostnames: str = Field(default="")  # comma-separated
fetch_user_content_max_response_bytes: int = Field(default=32_768, ge=1024, le=1_048_576)
fetch_user_content_timeout_seconds: float = Field(default=15.0, ge=1.0, le=60.0)
```

Both `enabled=true` AND a non-empty `hostnames` list are required. If either is missing, the tool returns `{"error": "fetch_user_content disabled by operator"}`.

### URL validation (`src/dlw/ai/url_guard.py`, new)

Five layered checks before any network I/O:

1. **Scheme**: must be `https`. Reject `http`, `file`, `data`, `ftp`, anything else.
2. **Parse**: must yield a hostname. Reject IPv6 literal brackets, userinfo (`user:pass@host`), unusual ports (only 443 allowed).
3. **Hostname allowlist**: hostname must be EXACTLY in the operator's whitelist (lowercase, exact string match; NO prefix/suffix matching — `evil.example.com` does NOT match whitelist `example.com`). Punycode is normalized via `idna` (already a transitive dep of httpx) before compare.
4. **IP-form rejection**: if the hostname parses as an IP literal (`ipaddress.ip_address(host)` succeeds), reject. Operators must whitelist hostnames, not raw IPs (forces DNS through a name we can audit).
5. **Resolved-IP check** (best-effort SSRF defense): resolve the hostname via `socket.getaddrinfo`; reject if ANY resolved address is private/loopback/link-local/multicast/reserved (per `ipaddress.ip_address(...).is_private / .is_loopback / .is_link_local / .is_multicast / .is_reserved / .is_unspecified`).

**Honest threat model:** the resolved-IP check happens BEFORE httpx connects, so DNS rebinding (server returns a public IP at validate-time, then a private IP when httpx resolves) is **not** prevented. The admin whitelist is the primary security boundary: operators MUST only whitelist hostnames they trust to never resolve to internal IPs. Documenting this honestly is better than claiming complete SSRF safety.

### Tool implementation (`src/dlw/ai/tools.py`)

New tool `fetch_user_content`, registered in `READONLY_TOOLS` (uses the SP4e-A choke point + standard flow). Pattern mirrors `_hf_model_card` (T2 trust, pre-wrap, `external_fields=[]`).

```python
async def _fetch_user_content(session, principal, *, url: str) -> dict:
    s = get_settings()
    if not s.fetch_user_content_enabled:
        return {"error": "fetch_user_content disabled by operator"}
    allow = [h.strip().lower() for h in s.fetch_user_content_hostnames.split(",") if h.strip()]
    if not allow:
        return {"error": "fetch_user_content disabled by operator (empty hostname allowlist)"}
    try:
        validated_url = validate_fetch_url(url, allow=allow)
    except UrlValidationError as e:
        return {"error": f"url_rejected: {e}"}
    try:
        resp = await _http_get(validated_url, timeout=s.fetch_user_content_timeout_seconds,
                               max_bytes=s.fetch_user_content_max_response_bytes)
    except FetchError as e:
        return {"error": f"fetch_failed: {e}"}
    res = sanitize_t2(resp.text, source=f"fetch:{validated_url}")
    return {
        "url": validated_url,
        "status_code": resp.status_code,
        "content_type": resp.content_type,
        "size_bytes": len(resp.text.encode("utf-8")),
        "sanitized": res.text,
        "warnings": res.warnings,
        "refused": res.refused,
    }
```

`READONLY_TOOLS` declaration:

```python
"fetch_user_content": Tool(
    "fetch_user_content",
    "Fetch the body of an HTTPS URL the user provided. Returns sanitized text "
    "(T2 trust). Operator-gated: requires DLW_AI_FETCH_USER_CONTENT_ENABLED=true "
    "and DLW_AI_FETCH_USER_CONTENT_HOSTNAMES set to a comma-separated host list.",
    {"type": "object", "required": ["url"], "properties": {
        "url": {"type": "string", "format": "uri"}}},
    _fetch_user_content,
    external_fields=[]),  # pre-wraps `sanitized` inline (mirror hf_*)
```

### HTTP fetch helper (`src/dlw/services/url_fetch.py`, new)

```python
async def _http_get(url: str, *, timeout: float, max_bytes: int) -> FetchResponse:
    async with httpx.AsyncClient(
        follow_redirects=False,  # SECURITY: never follow redirects (could escape whitelist).
        timeout=timeout,
        verify=True,  # explicit; default is True, declare intent.
    ) as client:
        try:
            resp = await client.get(url, headers={"Accept": "text/*, application/json"})
        except httpx.RequestError as e:
            raise FetchError(f"request_error: {type(e).__name__}") from e
        ct = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if not _is_allowed_content_type(ct):
            raise FetchError(f"content_type_rejected: {ct}")
        body = resp.content[:max_bytes]
        return FetchResponse(
            status_code=resp.status_code,
            content_type=ct,
            text=body.decode("utf-8", errors="replace"),
        )
```

Allowed content types: `text/plain`, `text/html`, `text/markdown`, `text/xml`, `application/json`, `application/xhtml+xml`. Reject everything else (images, binaries, octet-stream).

### What's deliberately NOT in scope

- **`web_search`**: requires a search-API operator choice (Tavily / Brave / SerpAPI / DuckDuckGo / etc.) — named follow-on; deferred.
- **POST / DELETE / etc.**: GET only.
- **Custom request headers**: not exposed to the LLM (no Authorization/Cookie pass-through, no User-Agent override).
- **Redirect following**: disabled. Forces the LLM to explicitly fetch each URL in a chain (more auditable; redirects could bypass the whitelist).
- **DNS-rebinding-proof fetching** (e.g., connect-by-IP custom transport): out of scope. Admin whitelist is the boundary; documented honestly.
- **Per-tenant whitelist**: global only; per-tenant overrides defer.
- **Caching**: every call fetches fresh.
- **Streaming responses**: 32 KB cap, single shot.

## §1 Threat model (documented honestly)

| Threat | Defense |
|---|---|
| Arbitrary egress to internal services (SSRF) | Scheme allowlist (https only), hostname allowlist, IP-form rejection, pre-fetch DNS check against private/loopback/link-local ranges |
| DNS rebinding | **NOT defended.** Operators must only whitelist hostnames they control or trust to never resolve to internal IPs. |
| Redirect escape | `follow_redirects=False` |
| Content injection | `sanitize_t2` wraps response body in `<external_user_content trust_level="t2">` boundary tag; T2 max 8 KB after sanitize |
| Body-size DoS | Hard cap at 32 KB (configurable up to 1 MB) |
| Slowloris | 15 s default timeout (configurable 1–60 s) |
| Binary / octet-stream / images | Content-Type allowlist (text/*, application/json, application/xhtml+xml only) |
| Credential exfiltration via URL userinfo | URL validator rejects URLs with `user:pass@` components |
| Port scanning (IPv4) | Pre-fetch DNS check + only port 443 allowed |
| Choke point bypass (T2 wrapping) | Tool pre-wraps inline (`sanitize_t2`); `external_fields=[]` so choke point is a no-op for this field (no double-wrap) |
| Error-message external content | Inherited from SP4e-A: `sanitize_error_key` always sanitizes `error` key at the choke point |

## §2 Tests

`tests/ai/test_url_guard.py` (new):
- `validate_fetch_url` accepts whitelisted hostname (https://example.com/path) → returns URL.
- Rejects http://example.com (scheme).
- Rejects ftp://example.com (scheme).
- Rejects https://example.com:8443 (non-443 port).
- Rejects https://user:pass@example.com/ (userinfo).
- Rejects https://192.168.1.1/ (IP literal).
- Rejects https://evil.example.com/ when whitelist is ["example.com"] (no prefix/suffix match).
- Rejects https://EXAMPLE.COM/ properly via case-insensitive compare (accepts as same as example.com).
- Rejects URL where `getaddrinfo` resolves to private IP (mock `socket.getaddrinfo`).
- Rejects URL where `getaddrinfo` resolves to loopback (mock).
- Accepts URL where `getaddrinfo` resolves to public IP (mock).

`tests/ai/test_fetch_user_content.py` (new):
- Disabled flag → returns `{"error": "...disabled by operator"}`.
- Empty hostname allowlist → returns `{"error": "...disabled by operator..."}`.
- URL rejected by validator → `{"error": "url_rejected: ..."}`.
- Successful fetch (mocked) → returns dict with `sanitized` starting with `<external_user_content`.
- Timeout (mocked `httpx.TimeoutException`) → `{"error": "fetch_failed: ..."}`.
- Disallowed content-type (e.g., `image/png`) → `{"error": "fetch_failed: content_type_rejected: ..."}`.
- Body size cap honored (response > max_bytes → truncated to max_bytes, no error).
- Sanitize T2 applied: response containing bidi override → `refused=True`.

## §3 Files

- **Modify** `src/dlw/config.py` — 4 new settings (`fetch_user_content_enabled`, `_hostnames`, `_max_response_bytes`, `_timeout_seconds`).
- **Create** `src/dlw/ai/url_guard.py` — `UrlValidationError`, `validate_fetch_url(url, *, allow) -> str`.
- **Create** `src/dlw/services/url_fetch.py` — `FetchError`, `FetchResponse`, `_http_get`, content-type allowlist.
- **Modify** `src/dlw/ai/tools.py` — add `_fetch_user_content` async function; register `fetch_user_content` in `READONLY_TOOLS`.
- **Create** `tests/ai/test_url_guard.py` — 10 validator tests.
- **Create** `tests/ai/test_fetch_user_content.py` — 8 tool tests (mock `_http_get` and `socket.getaddrinfo`).

Zero migration / openapi / frontend / executor change.

## §4 Notes

- Lint gate: `pytest -q` + `lint_invariants --strict`. No new invariant added in this PR (could add one in a follow-on: "any new tool with arbitrary egress must declare an admin gate + URL validator call").
- Admin docs (`docs/operator/`): not updated in this PR — deferred to a follow-on docs sweep along with `web_search`.
- The hostname allowlist is GLOBAL (across all tenants). Per-tenant overrides are a named follow-on.
- Once `web_search` lands, both tools should share the same `url_guard` + `url_fetch` helpers (already extracted in this PR for reuse).
