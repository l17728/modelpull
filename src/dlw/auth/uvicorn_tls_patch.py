"""uvicorn transport injection for mTLS peer-cert extraction (Phase 2 W3a).

uvicorn (httptools backend) does not expose the asyncio transport in the ASGI
``scope`` dict.  This module monkey-patches ``HttpToolsProtocol.on_headers_complete``
to inject ``scope["transport"] = self.transport`` before dispatching each request,
making the SSL transport available to ``_extract_peer_cert`` in executor_mtls.py.

Call ``install_transport_scope_patch()`` once at application startup (from
``main.lifespan``).  The patch is idempotent.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_patched = False


def install_transport_scope_patch() -> None:
    """Idempotently monkey-patch HttpToolsProtocol to inject the asyncio
    transport into the ASGI scope so mTLS peer certs are accessible."""
    global _patched
    if _patched:
        return
    try:
        from uvicorn.protocols.http.httptools_impl import HttpToolsProtocol

        _orig = HttpToolsProtocol.on_headers_complete

        def _patched_on_headers_complete(self) -> None:  # type: ignore[misc]
            _orig(self)
            # self.scope is the dict passed (by reference) to RequestResponseCycle
            # and ultimately to the ASGI app.  Injecting the transport here makes
            # it available as request.scope["transport"] inside FastAPI endpoints.
            if self.scope is not None:
                self.scope["transport"] = self.transport

        HttpToolsProtocol.on_headers_complete = _patched_on_headers_complete
        _patched = True
        logger.debug("installed uvicorn transport scope patch for mTLS peer-cert extraction")
    except ImportError:
        # httptools not available — uvicorn falls back to h11.
        # h11 path is rare (httptools is a dependency of uvicorn[standard])
        # and not supported for direct-TLS peer cert extraction.
        logger.warning(
            "httptools not available; uvicorn transport scope patch not installed. "
            "mTLS peer-cert extraction via direct TLS will not work. "
            "Use DLW_TLS_TRUSTED_PROXY=1 with a terminating proxy instead."
        )
