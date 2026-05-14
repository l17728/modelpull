"""mTLS peer-cert dependency (Phase 2 W3a §3.4)."""
from __future__ import annotations

import os

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.api.tasks import _session
from dlw.auth.ca import fingerprint_of
from dlw.db.models.executor import Executor


def _extract_peer_cert(request: Request) -> bytes | None:
    """Two paths: (a) direct uvicorn TLS — transport injected into scope by the
    HttpToolsProtocol patch in dlw.auth.uvicorn_tls_patch; (b) trusted-proxy
    forwarded header — only honored when DLW_TLS_TRUSTED_PROXY=1.

    uvicorn (httptools) does not expose the asyncio transport in the ASGI scope
    natively. The ``install_transport_scope_patch()`` call in ``main.lifespan``
    monkey-patches ``HttpToolsProtocol.on_headers_complete`` to inject
    ``scope["transport"]`` before the request is dispatched.  From the
    transport we reach ``ssl_object.getpeercert(binary_form=True)`` → DER bytes.
    """
    transport = request.scope.get("transport")
    if transport is not None and hasattr(transport, "get_extra_info"):
        # Prefer ssl_object.getpeercert(binary_form=True) — returns DER bytes.
        # transport.get_extra_info("peercert") returns a dict (parsed), not DER.
        ssl_obj = transport.get_extra_info("ssl_object")
        if ssl_obj is not None:
            try:
                peercert_der = ssl_obj.getpeercert(binary_form=True)
                if peercert_der:
                    cert = x509.load_der_x509_certificate(peercert_der)
                    return cert.public_bytes(serialization.Encoding.PEM)
            except Exception:
                pass
    if os.environ.get("DLW_TLS_TRUSTED_PROXY") == "1":
        header = request.headers.get("X-Client-Cert-PEM")
        if header:
            return header.replace("\\n", "\n").encode("utf-8")
    return None


async def require_executor_mtls(
    request: Request,
    session: AsyncSession = Depends(_session),
) -> Executor:
    """Validate mTLS peer cert + look up executor by fingerprint."""
    cert_pem = _extract_peer_cert(request)
    if cert_pem is None:
        raise HTTPException(401, detail="missing or invalid mTLS peer cert")
    try:
        fp = fingerprint_of(cert_pem)
    except Exception as e:
        raise HTTPException(401, detail=f"invalid client cert: {e}") from e
    ex = (await session.execute(
        select(Executor).where(Executor.cert_fingerprint == fp)
    )).scalar_one_or_none()
    if ex is None:
        raise HTTPException(401, detail="cert fingerprint not registered")
    return ex
