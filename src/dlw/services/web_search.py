"""SP4e follow-on: Brave Search API client (free tier, 2000 queries/month).

Fixed endpoint — no operator-supplied URL, so the SSRF-style URL-allowlist
guard from url_guard.py is not needed here. Uses Accept-Encoding: identity
to share the SP4e-B decompression-bomb mitigation."""
from __future__ import annotations

from dataclasses import dataclass

import httpx

BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


class WebSearchError(RuntimeError):
    """Raised when the Brave Search API call fails or returns an error."""


@dataclass
class WebSearchResult:
    title: str
    url: str
    description: str


async def search_brave(
    query: str, *, api_key: str, count: int = 5, timeout: float = 10.0,
) -> list[WebSearchResult]:
    """Call the Brave Search API and return a list of result records.

    Translates network errors and non-2xx responses into typed
    WebSearchError messages (request_error / invalid_api_key / rate_limited
    / http_<code> / invalid_json) so the caller can fold them into a
    structured tool error."""
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
            raise WebSearchError(
                f"request_error: {type(e).__name__}") from e
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
        out: list[WebSearchResult] = []
        for item in (data.get("web", {}).get("results") or []):
            out.append(WebSearchResult(
                title=str(item.get("title") or ""),
                url=str(item.get("url") or ""),
                description=str(item.get("description") or ""),
            ))
        return out[:count]
