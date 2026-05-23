# SP4e follow-on B — `fetch_user_content` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: subagent-driven-development / executing-plans. Checkbox steps.

**Goal:** New `fetch_user_content` AI tool with admin-gate + hostname-whitelist + IP-form rejection + pre-fetch DNS check + T2 sanitize. Reuses SP4e-A choke point. `web_search` is out of scope (deferred — needs search-API operator choice).

**Spec:** `docs/superpowers/specs/2026-05-23-sp4e-fetch-user-content-design.md`

**Locked constraints:**
- **Disabled by default** (`DLW_AI_FETCH_USER_CONTENT_ENABLED=false`). EMPTY hostname allowlist also fully disables (no allowed host → every URL rejected).
- **HTTPS scheme only**, port 443 only, no userinfo, no IP literals as hostname.
- **Hostname allowlist is EXACT lowercase match** (no prefix/suffix — `evil.example.com` MUST NOT match whitelist `example.com`).
- **DNS check** before fetch: reject any URL whose hostname resolves to private/loopback/link-local/multicast/reserved IP.
- **No redirect following** (`follow_redirects=False`) — security boundary.
- **Content-Type allowlist**: `text/plain`, `text/html`, `text/markdown`, `text/xml`, `application/json`, `application/xhtml+xml`. Reject all others.
- **32 KB default body cap** (configurable 1 KB – 1 MB).
- **15 s default timeout** (configurable 1–60 s).
- **GET only**, no custom request headers passed through.
- **T2 sanitize inline** (`sanitize_t2`); `external_fields=[]` so SP4e-A choke point is no-op for the pre-wrapped `sanitized` field (avoid double-wrap of arbitrary user content).
- Tool registered in `READONLY_TOOLS`. The SP4e-A choke point handles the `error` key unconditionally.
- DNS rebinding is NOT defended — admin whitelist is the boundary (documented honestly).
- Zero migration / openapi / frontend / executor change.
- Lint gates: `uv run pytest -q` + `uv run python tools/lint_invariants.py --strict`.

---

## File Structure

- **Modify** `src/dlw/config.py` — 4 new settings.
- **Create** `src/dlw/ai/url_guard.py` — `validate_fetch_url` + `UrlValidationError`.
- **Create** `src/dlw/services/url_fetch.py` — `_http_get` + `FetchError` + `FetchResponse`.
- **Modify** `src/dlw/ai/tools.py` — add `_fetch_user_content` + register in `READONLY_TOOLS`.
- **Create** `tests/ai/test_url_guard.py` — 11 validator tests.
- **Create** `tests/ai/test_fetch_user_content.py` — 8 tool tests.

---

## Milestone M1 — URL validator + tests

### Task 1: `url_guard.py` + tests

**Files:** new `src/dlw/ai/url_guard.py`, new `tests/ai/test_url_guard.py`.

- [ ] **Step 1 (failing tests):** create `tests/ai/test_url_guard.py`:

  ```python
  """SP4e follow-on B: URL allowlist + SSRF validation tests."""
  from __future__ import annotations

  from unittest.mock import patch

  import pytest

  from dlw.ai.url_guard import UrlValidationError, validate_fetch_url

  ALLOW = ["example.com", "docs.python.org"]


  def _resolve(*ips):
      """Make a fake socket.getaddrinfo that returns the given IPs."""
      return [(0, 0, 0, "", (ip, 0)) for ip in ips]


  def test_accepts_whitelisted_https():
      with patch("socket.getaddrinfo", return_value=_resolve("93.184.216.34")):
          out = validate_fetch_url("https://example.com/path?q=1", allow=ALLOW)
      assert out == "https://example.com/path?q=1"


  def test_accepts_case_insensitive_hostname():
      with patch("socket.getaddrinfo", return_value=_resolve("93.184.216.34")):
          out = validate_fetch_url("https://EXAMPLE.COM/x", allow=ALLOW)
      # URL hostname is normalized to lowercase in the returned string.
      assert out.lower().startswith("https://example.com/")


  def test_rejects_http_scheme():
      with pytest.raises(UrlValidationError, match="scheme"):
          validate_fetch_url("http://example.com/", allow=ALLOW)


  def test_rejects_ftp_scheme():
      with pytest.raises(UrlValidationError, match="scheme"):
          validate_fetch_url("ftp://example.com/", allow=ALLOW)


  def test_rejects_non_443_port():
      with pytest.raises(UrlValidationError, match="port"):
          validate_fetch_url("https://example.com:8443/", allow=ALLOW)


  def test_rejects_userinfo():
      with pytest.raises(UrlValidationError, match="userinfo"):
          validate_fetch_url("https://u:p@example.com/", allow=ALLOW)


  def test_rejects_ip_literal_hostname():
      with pytest.raises(UrlValidationError, match="ip_literal"):
          validate_fetch_url("https://192.168.1.1/", allow=ALLOW)


  def test_rejects_ipv6_literal_hostname():
      with pytest.raises(UrlValidationError, match="ip_literal"):
          validate_fetch_url("https://[::1]/", allow=ALLOW)


  def test_rejects_hostname_not_in_allowlist():
      with pytest.raises(UrlValidationError, match="not_in_allowlist"):
          validate_fetch_url("https://evil.example.com/", allow=ALLOW)


  def test_rejects_hostname_prefix_substring_attack():
      """`evil.example.com` must NOT match allowlist `example.com`."""
      with pytest.raises(UrlValidationError, match="not_in_allowlist"):
          validate_fetch_url("https://evil.example.com/", allow=["example.com"])


  def test_rejects_dns_resolved_to_private_ip():
      with patch("socket.getaddrinfo", return_value=_resolve("10.0.0.5")):
          with pytest.raises(UrlValidationError, match="resolved_to_private"):
              validate_fetch_url("https://example.com/", allow=ALLOW)


  def test_rejects_dns_resolved_to_loopback():
      with patch("socket.getaddrinfo", return_value=_resolve("127.0.0.1")):
          with pytest.raises(UrlValidationError, match="resolved_to_private"):
              validate_fetch_url("https://example.com/", allow=ALLOW)


  def test_rejects_dns_resolved_to_link_local():
      with patch("socket.getaddrinfo", return_value=_resolve("169.254.169.254")):
          with pytest.raises(UrlValidationError, match="resolved_to_private"):
              validate_fetch_url("https://example.com/", allow=ALLOW)


  def test_rejects_if_any_resolved_ip_is_private():
      """Even one bad IP in the resolved set fails (defense vs split-resolution)."""
      with patch("socket.getaddrinfo",
                 return_value=_resolve("93.184.216.34", "10.0.0.5")):
          with pytest.raises(UrlValidationError, match="resolved_to_private"):
              validate_fetch_url("https://example.com/", allow=ALLOW)
  ```

- [ ] **Step 2: verify FAIL** — `uv run pytest tests/ai/test_url_guard.py -v` → `ImportError`.

- [ ] **Step 3 (implement url_guard.py):** create `src/dlw/ai/url_guard.py`:

  ```python
  """SP4e follow-on B: URL allowlist + SSRF pre-fetch validation for the
  fetch_user_content AI tool. Honest threat model: this does NOT defend
  against DNS rebinding (the resolved-IP check happens before httpx connects).
  The admin hostname allowlist is the primary security boundary."""
  from __future__ import annotations

  import ipaddress
  import socket
  from urllib.parse import urlparse, urlunparse


  class UrlValidationError(ValueError):
      """Raised when a URL fails validation."""


  def _is_ip_literal(host: str) -> bool:
      try:
          ipaddress.ip_address(host.strip("[]"))
          return True
      except ValueError:
          return False


  def _is_unsafe_ip(ip_str: str) -> bool:
      try:
          ip = ipaddress.ip_address(ip_str)
      except ValueError:
          return True  # unparseable IP — refuse on principle
      return (ip.is_private or ip.is_loopback or ip.is_link_local
              or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


  def validate_fetch_url(url: str, *, allow: list[str]) -> str:
      """Validate the URL passes all five SSRF defense layers. Returns the
      normalized URL (lowercase hostname). Raises UrlValidationError on
      any failure."""
      try:
          parsed = urlparse(url)
      except ValueError as e:
          raise UrlValidationError(f"unparseable: {e}") from e

      # 1. Scheme
      if parsed.scheme != "https":
          raise UrlValidationError(f"scheme: only https allowed (got {parsed.scheme!r})")

      # 2. Parse hygiene
      if parsed.username or parsed.password:
          raise UrlValidationError("userinfo not allowed")
      if parsed.port not in (None, 443):
          raise UrlValidationError(f"port: only 443 allowed (got {parsed.port})")
      host = (parsed.hostname or "").lower()
      if not host:
          raise UrlValidationError("missing hostname")

      # 4. IP-literal rejection (force hostnames)
      if _is_ip_literal(host):
          raise UrlValidationError(f"ip_literal not allowed: {host!r}")

      # 3. Hostname allowlist (EXACT match, lowercase)
      allow_norm = {h.lower() for h in allow if h}
      if host not in allow_norm:
          raise UrlValidationError(f"hostname not_in_allowlist: {host!r}")

      # 5. Resolve + check every IP
      try:
          infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
      except socket.gaierror as e:
          raise UrlValidationError(f"dns_failed: {e}") from e
      ips = {info[4][0] for info in infos}
      if not ips:
          raise UrlValidationError("dns_no_results")
      for ip in ips:
          if _is_unsafe_ip(ip):
              raise UrlValidationError(f"resolved_to_private/loopback/etc: {ip!r}")

      # Return URL with hostname lowercased (preserve everything else)
      normalized = parsed._replace(netloc=host if parsed.port is None
                                   else f"{host}:{parsed.port}")
      return urlunparse(normalized)
  ```

- [ ] **Step 4: verify PASS** — `uv run pytest tests/ai/test_url_guard.py -v` → all 14 pass.

- [ ] **Step 5: tidy + commit:**
  ```bash
  cd "D:/download_weights"
  uv run ruff check --select I001 --fix src/dlw/ai/url_guard.py tests/ai/test_url_guard.py
  git add src/dlw/ai/url_guard.py tests/ai/test_url_guard.py
  git commit -m "feat(sp4e-B): url_guard with allowlist + SSRF pre-fetch DNS check"
  ```

### Task 2: M1 gate
- [ ] `uv run pytest tests/ai/ -q` — all pass. No commit.

---

## Milestone M2 — HTTP fetch helper + tool + tests

### Task 3: url_fetch service + fetch_user_content tool

**Files:** new `src/dlw/services/url_fetch.py`, `src/dlw/config.py`, `src/dlw/ai/tools.py`, new `tests/ai/test_fetch_user_content.py`.

- [ ] **Step 1 (config additions):** in `src/dlw/config.py` (after the FU6 device_* settings, around line 88):

  ```python
  # SP4e follow-on B: fetch_user_content AI tool — arbitrary egress.
  # DEFAULT OFF. Empty hostname list = effective disable even if enabled.
  fetch_user_content_enabled: bool = Field(default=False)
  fetch_user_content_hostnames: str = Field(default="")  # comma-separated
  fetch_user_content_max_response_bytes: int = Field(
      default=32_768, ge=1024, le=1_048_576)
  fetch_user_content_timeout_seconds: float = Field(
      default=15.0, ge=1.0, le=60.0)
  ```

- [ ] **Step 2 (failing tool tests):** create `tests/ai/test_fetch_user_content.py`:

  ```python
  """SP4e follow-on B: fetch_user_content tool tests."""
  from __future__ import annotations

  from unittest.mock import patch

  import pytest

  from dlw.ai.tools import READONLY_TOOLS
  from dlw.config import get_settings
  from dlw.services.url_fetch import FetchError, FetchResponse


  def _principal(uid: int = 1):
      from dlw.auth.principal import Principal
      return Principal(user_id=uid, tenant_id=1, role="tenant_operator",
                       project_ids=frozenset(), is_service=False)


  @pytest.fixture(autouse=True)
  def _clean_settings(monkeypatch):
      monkeypatch.delenv("DLW_FETCH_USER_CONTENT_ENABLED", raising=False)
      monkeypatch.delenv("DLW_FETCH_USER_CONTENT_HOSTNAMES", raising=False)
      get_settings.cache_clear()
      yield
      get_settings.cache_clear()


  @pytest.mark.asyncio
  async def test_disabled_by_default_returns_error():
      tool = READONLY_TOOLS["fetch_user_content"]
      out = await tool.run(None, _principal(), url="https://example.com/")
      assert "disabled by operator" in out["error"]


  @pytest.mark.asyncio
  async def test_enabled_but_empty_allowlist_returns_error(monkeypatch):
      monkeypatch.setenv("DLW_FETCH_USER_CONTENT_ENABLED", "true")
      monkeypatch.setenv("DLW_FETCH_USER_CONTENT_HOSTNAMES", "")
      get_settings.cache_clear()
      tool = READONLY_TOOLS["fetch_user_content"]
      out = await tool.run(None, _principal(), url="https://example.com/")
      assert "disabled by operator" in out["error"]


  @pytest.mark.asyncio
  async def test_url_rejected_by_validator(monkeypatch):
      monkeypatch.setenv("DLW_FETCH_USER_CONTENT_ENABLED", "true")
      monkeypatch.setenv("DLW_FETCH_USER_CONTENT_HOSTNAMES", "example.com")
      get_settings.cache_clear()
      tool = READONLY_TOOLS["fetch_user_content"]
      out = await tool.run(None, _principal(), url="http://example.com/")
      assert "url_rejected" in out["error"]


  @pytest.mark.asyncio
  async def test_successful_fetch_returns_sanitized(monkeypatch):
      monkeypatch.setenv("DLW_FETCH_USER_CONTENT_ENABLED", "true")
      monkeypatch.setenv("DLW_FETCH_USER_CONTENT_HOSTNAMES", "example.com")
      get_settings.cache_clear()

      async def fake_get(url, *, timeout, max_bytes):
          return FetchResponse(status_code=200, content_type="text/plain",
                               text="hello world")

      with patch("dlw.ai.tools._http_get", fake_get), \
           patch("socket.getaddrinfo",
                 return_value=[(0, 0, 0, "", ("93.184.216.34", 0))]):
          tool = READONLY_TOOLS["fetch_user_content"]
          out = await tool.run(None, _principal(), url="https://example.com/")

      assert out["status_code"] == 200
      assert out["content_type"] == "text/plain"
      assert out["sanitized"].startswith("<external_user_content")
      assert "hello world" in out["sanitized"]


  @pytest.mark.asyncio
  async def test_fetch_timeout_returns_error(monkeypatch):
      monkeypatch.setenv("DLW_FETCH_USER_CONTENT_ENABLED", "true")
      monkeypatch.setenv("DLW_FETCH_USER_CONTENT_HOSTNAMES", "example.com")
      get_settings.cache_clear()

      async def fake_get(url, *, timeout, max_bytes):
          raise FetchError("request_error: TimeoutException")

      with patch("dlw.ai.tools._http_get", fake_get), \
           patch("socket.getaddrinfo",
                 return_value=[(0, 0, 0, "", ("93.184.216.34", 0))]):
          tool = READONLY_TOOLS["fetch_user_content"]
          out = await tool.run(None, _principal(), url="https://example.com/")
      assert "fetch_failed" in out["error"]
      assert "TimeoutException" in out["error"]


  @pytest.mark.asyncio
  async def test_disallowed_content_type_via_helper(monkeypatch):
      """When _http_get itself raises content_type_rejected."""
      monkeypatch.setenv("DLW_FETCH_USER_CONTENT_ENABLED", "true")
      monkeypatch.setenv("DLW_FETCH_USER_CONTENT_HOSTNAMES", "example.com")
      get_settings.cache_clear()

      async def fake_get(url, *, timeout, max_bytes):
          raise FetchError("content_type_rejected: image/png")

      with patch("dlw.ai.tools._http_get", fake_get), \
           patch("socket.getaddrinfo",
                 return_value=[(0, 0, 0, "", ("93.184.216.34", 0))]):
          tool = READONLY_TOOLS["fetch_user_content"]
          out = await tool.run(None, _principal(), url="https://example.com/")
      assert "content_type_rejected" in out["error"]


  @pytest.mark.asyncio
  async def test_t2_refusal_on_bidi_override(monkeypatch):
      monkeypatch.setenv("DLW_FETCH_USER_CONTENT_ENABLED", "true")
      monkeypatch.setenv("DLW_FETCH_USER_CONTENT_HOSTNAMES", "example.com")
      get_settings.cache_clear()

      async def fake_get(url, *, timeout, max_bytes):
          return FetchResponse(status_code=200, content_type="text/plain",
                               text="prefix‮malicious")  # RTL override

      with patch("dlw.ai.tools._http_get", fake_get), \
           patch("socket.getaddrinfo",
                 return_value=[(0, 0, 0, "", ("93.184.216.34", 0))]):
          tool = READONLY_TOOLS["fetch_user_content"]
          out = await tool.run(None, _principal(), url="https://example.com/")
      assert out["refused"] is True
      assert any("Bidi" in w for w in out["warnings"])


  def test_tool_registered_with_empty_external_fields():
      """fetch_user_content pre-wraps `sanitized` inline; external_fields=[]
      so SP4e-A choke point is a no-op (avoids double-wrap)."""
      tool = READONLY_TOOLS["fetch_user_content"]
      assert tool.external_fields == [], (
          "fetch_user_content pre-wraps inline; external_fields must be empty")
  ```

- [ ] **Step 3: verify FAIL** — `uv run pytest tests/ai/test_fetch_user_content.py -v` → ImportError or missing tool.

- [ ] **Step 4 (implement url_fetch.py):** create `src/dlw/services/url_fetch.py`:

  ```python
  """SP4e follow-on B: bounded HTTP GET helper for fetch_user_content."""
  from __future__ import annotations

  from dataclasses import dataclass

  import httpx

  _ALLOWED_CTS = frozenset({
      "text/plain", "text/html", "text/markdown", "text/xml",
      "application/json", "application/xhtml+xml",
  })


  class FetchError(RuntimeError):
      """Raised when an HTTP GET fails or returns disallowed content."""


  @dataclass
  class FetchResponse:
      status_code: int
      content_type: str
      text: str


  def _is_allowed_content_type(ct: str) -> bool:
      return ct in _ALLOWED_CTS


  async def _http_get(url: str, *, timeout: float, max_bytes: int) -> FetchResponse:
      async with httpx.AsyncClient(
          follow_redirects=False,  # SECURITY: never follow redirects.
          timeout=timeout,
          verify=True,
      ) as client:
          try:
              resp = await client.get(
                  url, headers={"Accept": "text/*, application/json"})
          except httpx.RequestError as e:
              raise FetchError(f"request_error: {type(e).__name__}") from e
      ct = resp.headers.get("content-type", "").split(";")[0].strip().lower()
      if not _is_allowed_content_type(ct):
          raise FetchError(f"content_type_rejected: {ct or 'unknown'}")
      body = resp.content[:max_bytes]
      return FetchResponse(
          status_code=resp.status_code,
          content_type=ct,
          text=body.decode("utf-8", errors="replace"),
      )
  ```

- [ ] **Step 5 (implement tool):** in `src/dlw/ai/tools.py`:

  a) Add imports at top (with the other `dlw.ai.*` imports — keep ordering tidy):
  ```python
  from dlw.ai.url_guard import UrlValidationError, validate_fetch_url
  from dlw.services.url_fetch import FetchError, FetchResponse, _http_get
  ```

  b) Add the tool function (after `_hf_model_card` ~line 127):
  ```python
  async def _fetch_user_content(session, principal, *, url: str) -> dict:
      """SP4e follow-on B: arbitrary egress AI tool. Disabled by default;
      operator must set DLW_FETCH_USER_CONTENT_ENABLED=true AND
      DLW_FETCH_USER_CONTENT_HOSTNAMES to a non-empty comma-separated list.
      The hostname allowlist is the primary security boundary (DNS rebinding
      is not defended — operators MUST trust whitelisted hosts to never
      resolve to internal IPs)."""
      s = get_settings()
      if not s.fetch_user_content_enabled:
          return {"error": "fetch_user_content disabled by operator"}
      allow = [h.strip().lower() for h in
               s.fetch_user_content_hostnames.split(",") if h.strip()]
      if not allow:
          return {"error": "fetch_user_content disabled by operator "
                           "(empty hostname allowlist)"}
      try:
          validated = validate_fetch_url(url, allow=allow)
      except UrlValidationError as e:
          return {"error": f"url_rejected: {e}"}
      try:
          resp = await _http_get(
              validated, timeout=s.fetch_user_content_timeout_seconds,
              max_bytes=s.fetch_user_content_max_response_bytes)
      except FetchError as e:
          return {"error": f"fetch_failed: {e}"}
      res = sanitize_t2(resp.text, source=f"fetch:{validated}")
      return {
          "url": validated,
          "status_code": resp.status_code,
          "content_type": resp.content_type,
          "size_bytes": len(resp.text.encode("utf-8")),
          "sanitized": res.text,
          "warnings": res.warnings,
          "refused": res.refused,
      }
  ```

  c) Add tool registration to `READONLY_TOOLS` dict (after `hf_model_card`):
  ```python
  "fetch_user_content": Tool(
      "fetch_user_content",
      "Fetch the body of an HTTPS URL the user provided. Returns sanitized text "
      "(T2 trust). Operator-gated: requires DLW_AI_FETCH_USER_CONTENT_ENABLED=true "
      "and a non-empty DLW_AI_FETCH_USER_CONTENT_HOSTNAMES allowlist.",
      {"type": "object", "required": ["url"], "properties": {
          "url": {"type": "string", "format": "uri"}}},
      _fetch_user_content),
  ```

  (Note: `external_fields` omitted → default `[]`. The pre-wrap is inline via `sanitize_t2`; choke point is no-op for the `sanitized` field, which is correct since double-wrap would be ugly.)

- [ ] **Step 6: verify PASS** — `uv run pytest tests/ai/ -v` → all pass (new 8 tests + existing all pass).

- [ ] **Step 7 (full backend gate):** `uv run pytest -q` — full suite green. `uv run python tools/lint_invariants.py --strict` OK.

- [ ] **Step 8: tidy + commit:**
  ```bash
  uv run ruff check --select I001 --fix src/dlw/config.py src/dlw/ai/tools.py src/dlw/services/url_fetch.py tests/ai/test_fetch_user_content.py
  git add src/dlw/config.py src/dlw/ai/tools.py src/dlw/services/url_fetch.py tests/ai/test_fetch_user_content.py
  git commit -m "feat(sp4e-B): fetch_user_content AI tool + url_fetch helper"
  ```

### Task 4: M2 full gate
- [ ] `uv run pytest -q` — full suite green. `uv run python tools/lint_invariants.py --strict` OK. No commit.

---

## Self-Review

- **Spec coverage:** 5-layer URL validator → Task 1 Step 3 ✓; admin gate (`enabled` + non-empty `hostnames`) → Task 3 Step 5b first two checks ✓; T2 sanitize inline pre-wrap → Step 5b `sanitize_t2(...)` ✓; `external_fields=[]` default (no double-wrap) → Step 5c omitted-kwarg ✓; no redirects → Step 4 `follow_redirects=False` ✓; content-type allowlist → Step 4 `_is_allowed_content_type` ✓; tests for all 5 validator layers + 8 tool scenarios ✓; honest DNS-rebinding documentation → Step 3 docstring + spec §1 ✓.
- **Placeholder scan:** all code blocks concrete. Settings env var names use the `DLW_` prefix; correspondingly tests use `DLW_FETCH_USER_CONTENT_*` (pydantic `env_prefix="DLW_"`).
- **Type consistency:** `validate_fetch_url(str, *, list[str]) -> str`; `FetchResponse` dataclass with str/int/str fields; `_fetch_user_content` returns `dict`; tool registration uses positional args matching existing READONLY_TOOLS style.
- **Open risks for reviewers:** (a) DNS rebinding not defended — documented in spec §1 + tool docstring; admin whitelist is the boundary. (b) `urlparse` is from `urllib.parse` (stdlib); doesn't validate everything (e.g., it accepts some malformed URLs that aren't really URLs). The 5-layer check catches the common cases but a determined attacker who knows urlparse's quirks could find edge cases — pre-review should specifically check this. (c) Sanitize source uses `f"fetch:{validated_url}"` — the URL goes through `_escape_attr` in sanitize.py, so embedded `"`/`<` are encoded. (d) `monkeypatch.setenv` in tests requires `get_settings.cache_clear()` before AND after to defeat the LRU cache (autouse fixture handles this).
