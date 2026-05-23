"""SP4e follow-on B: URL allowlist + SSRF pre-fetch validation. Honest
threat model: this does NOT defend against DNS rebinding — the resolved-IP
check happens BEFORE httpx connects. The admin hostname allowlist is the
primary security boundary; operators MUST trust whitelisted hosts to never
resolve to internal IPs."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse, urlunparse


class UrlValidationError(ValueError):
    """Raised when a URL fails any of the five validation layers."""


def _normalize_host(h: str) -> str:
    """Normalize for allowlist compare: lowercase, strip trailing dot,
    IDNA-encode to ASCII (rejects Unicode homoglyphs by mapping them to
    distinct xn--... punycode that won't match a Latin allowlist entry)."""
    h = h.strip().lower().rstrip(".")
    if not h:
        return ""
    try:
        # idna package is a transitive dep of httpx; also stdlib `encode('idna')`
        # works for most cases. Use stdlib to avoid a new direct dependency.
        return h.encode("idna").decode("ascii").lower()
    except (UnicodeError, UnicodeDecodeError):
        # Unencodable host (e.g., empty labels) — let the allowlist check fail
        # downstream with a clear message.
        return h


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _is_unsafe_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


async def validate_fetch_url(url: str, *, allow: list[str]) -> str:
    """Async validator. Returns the normalized URL (lowercase host, no
    trailing dot, no IDNA changes to non-IDN hosts). Raises UrlValidationError
    on any failure."""
    # Layer 0: catch any urlparse / port-access ValueError as our typed error.
    try:
        parsed = urlparse(url)
        # Trigger any lazy ValueError from .port (e.g., port > 65535).
        port = parsed.port
        host_raw = parsed.hostname
        user = parsed.username
        pwd = parsed.password
    except ValueError as e:
        raise UrlValidationError(f"unparseable: {e}") from e

    # Layer 1: scheme.
    if parsed.scheme != "https":
        raise UrlValidationError(
            f"scheme: only https allowed (got {parsed.scheme!r})")

    # Layer 2: parse hygiene.
    if user or pwd:
        raise UrlValidationError("userinfo not allowed in URL")
    if port not in (None, 443):
        raise UrlValidationError(f"port: only 443 allowed (got {port})")
    if not host_raw:
        raise UrlValidationError("missing hostname")

    # Layer 3: IP-literal rejection.
    if _is_ip_literal(host_raw):
        raise UrlValidationError(f"ip_literal not allowed: {host_raw!r}")

    # Layer 4: hostname allowlist (IDNA-normalized, trailing-dot-stripped).
    host_norm = _normalize_host(host_raw)
    allow_norm = {_normalize_host(h) for h in allow if h.strip()}
    if host_norm not in allow_norm:
        raise UrlValidationError(f"hostname not_in_allowlist: {host_norm!r}")

    # Layer 5: async DNS check (every resolved IP must be public).
    try:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(
            host_norm, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise UrlValidationError(f"dns_failed: {e}") from e
    ips = {info[4][0] for info in infos}
    if not ips:
        raise UrlValidationError("dns_no_results")
    for ip in ips:
        if _is_unsafe_ip(ip):
            raise UrlValidationError(
                f"resolved_to_private/loopback/etc: {ip!r}")

    # Return URL with normalized host (preserves path/query/fragment).
    netloc = host_norm if port is None else f"{host_norm}:{port}"
    return urlunparse(parsed._replace(netloc=netloc))
