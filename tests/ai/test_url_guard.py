"""SP4e-B: URL allowlist + SSRF validation tests."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from dlw.ai.url_guard import UrlValidationError, validate_fetch_url

ALLOW = ["example.com", "docs.python.org"]


def _resolve_async(*ips):
    """Make a fake loop.getaddrinfo coroutine returning the given IPs."""
    async def _fake(_host, _port, **_kw):
        return [(0, 0, 0, "", (ip, 0)) for ip in ips]
    return _fake


async def test_accepts_whitelisted_https():
    with patch("asyncio.get_running_loop") as gl:
        gl.return_value.getaddrinfo = _resolve_async("93.184.216.34")
        out = await validate_fetch_url("https://example.com/path?q=1", allow=ALLOW)
    assert out.startswith("https://example.com/")


async def test_accepts_case_insensitive_hostname():
    with patch("asyncio.get_running_loop") as gl:
        gl.return_value.getaddrinfo = _resolve_async("93.184.216.34")
        out = await validate_fetch_url("https://EXAMPLE.COM/x", allow=ALLOW)
    assert "example.com" in out and "EXAMPLE.COM" not in out


async def test_accepts_trailing_dot_input_with_bare_allowlist():
    """Pre-review R1 B2: example.com. (FQDN form) MUST behave same as bare."""
    with patch("asyncio.get_running_loop") as gl:
        gl.return_value.getaddrinfo = _resolve_async("93.184.216.34")
        out = await validate_fetch_url("https://example.com./x", allow=ALLOW)
    assert "example.com" in out


async def test_accepts_bare_input_with_trailing_dot_allowlist():
    """Pre-review R1 B2: bare input with FQDN allowlist entry works too."""
    with patch("asyncio.get_running_loop") as gl:
        gl.return_value.getaddrinfo = _resolve_async("93.184.216.34")
        out = await validate_fetch_url("https://example.com/x", allow=["example.com."])
    assert "example.com" in out


async def test_rejects_http_scheme():
    with pytest.raises(UrlValidationError, match="scheme"):
        await validate_fetch_url("http://example.com/", allow=ALLOW)


async def test_rejects_ftp_scheme():
    with pytest.raises(UrlValidationError, match="scheme"):
        await validate_fetch_url("ftp://example.com/", allow=ALLOW)


async def test_rejects_non_443_port():
    with pytest.raises(UrlValidationError, match="port"):
        await validate_fetch_url("https://example.com:8443/", allow=ALLOW)


async def test_rejects_userinfo():
    with pytest.raises(UrlValidationError, match="userinfo"):
        await validate_fetch_url("https://u:p@example.com/", allow=ALLOW)


async def test_rejects_ip_literal_hostname():
    with pytest.raises(UrlValidationError, match="ip_literal"):
        await validate_fetch_url("https://192.168.1.1/", allow=ALLOW)


async def test_rejects_ipv6_literal_hostname():
    with pytest.raises(UrlValidationError, match="ip_literal"):
        await validate_fetch_url("https://[::1]/", allow=ALLOW)


async def test_rejects_hostname_not_in_allowlist():
    with pytest.raises(UrlValidationError, match="not_in_allowlist"):
        await validate_fetch_url("https://evil.example.com/", allow=ALLOW)


async def test_rejects_hostname_substring_attack():
    with pytest.raises(UrlValidationError, match="not_in_allowlist"):
        await validate_fetch_url("https://evil.example.com/", allow=["example.com"])


async def test_rejects_idna_homoglyph():
    """Pre-review R1 B3: Cyrillic 'а' in 'exаmple.com' must not match Latin
    allowlist entry 'example.com'. IDNA encodes the Cyrillic form to a
    distinct xn--... punycode string."""
    with pytest.raises(UrlValidationError, match="not_in_allowlist"):
        # Hostname with U+0430 CYRILLIC SMALL LETTER A in place of 'a'.
        await validate_fetch_url("https://exаmple.com/", allow=["example.com"])


async def test_rejects_malformed_port_via_value_error():
    """Pre-review R2 I4: parsed.port can raise ValueError for >65535."""
    with pytest.raises(UrlValidationError, match="unparseable|port"):
        await validate_fetch_url("https://example.com:99999/", allow=ALLOW)


async def test_rejects_dns_resolved_to_private_ip():
    with patch("asyncio.get_running_loop") as gl:
        gl.return_value.getaddrinfo = _resolve_async("10.0.0.5")
        with pytest.raises(UrlValidationError, match="resolved_to_private"):
            await validate_fetch_url("https://example.com/", allow=ALLOW)


async def test_rejects_dns_resolved_to_loopback():
    with patch("asyncio.get_running_loop") as gl:
        gl.return_value.getaddrinfo = _resolve_async("127.0.0.1")
        with pytest.raises(UrlValidationError, match="resolved_to_private"):
            await validate_fetch_url("https://example.com/", allow=ALLOW)


async def test_rejects_dns_resolved_to_link_local_aws_metadata():
    """169.254.169.254 = AWS/GCP/Azure metadata service IP."""
    with patch("asyncio.get_running_loop") as gl:
        gl.return_value.getaddrinfo = _resolve_async("169.254.169.254")
        with pytest.raises(UrlValidationError, match="resolved_to_private"):
            await validate_fetch_url("https://example.com/", allow=ALLOW)


async def test_rejects_if_any_resolved_ip_is_private():
    with patch("asyncio.get_running_loop") as gl:
        gl.return_value.getaddrinfo = _resolve_async("93.184.216.34", "10.0.0.5")
        with pytest.raises(UrlValidationError, match="resolved_to_private"):
            await validate_fetch_url("https://example.com/", allow=ALLOW)


async def test_rejects_mapped_ipv4_in_ipv6_private():
    """::ffff:10.0.0.1 (IPv4-mapped IPv6) classifies as private — confirm
    our check handles the dual-stack case (pre-review R1 L7)."""
    with patch("asyncio.get_running_loop") as gl:
        gl.return_value.getaddrinfo = _resolve_async("::ffff:10.0.0.1")
        with pytest.raises(UrlValidationError, match="resolved_to_private"):
            await validate_fetch_url("https://example.com/", allow=ALLOW)
