"""HMAC heartbeat dependency (Phase 2 W3a §3.4)."""
from __future__ import annotations

import time

from fastapi import Depends, Header, HTTPException, Request

from dlw.auth.executor_jwt_dep import require_executor_jwt
from dlw.auth.hmac_nonce import verify_hmac
from dlw.db.models.executor import Executor

_TIMESTAMP_SKEW_SECONDS = 300


async def require_hmac_heartbeat(
    request: Request,
    x_hmac_timestamp: int = Header(..., alias="X-HMAC-Timestamp"),
    x_hmac_nonce: str = Header(..., alias="X-HMAC-Nonce"),
    x_hmac_signature: str = Header(..., alias="X-HMAC-Signature"),
    ex: Executor = Depends(require_executor_jwt),
) -> Executor:
    now = int(time.time())
    if abs(now - x_hmac_timestamp) > _TIMESTAMP_SKEW_SECONDS:
        raise HTTPException(401, detail="CLOCK_SKEW")
    store = request.app.state.nonce_store
    if store.seen(x_hmac_nonce):
        raise HTTPException(401, detail="REPLAY_DETECTED")
    if ex.hmac_seed_encrypted is None:
        raise HTTPException(401, detail="HMAC_SEED_MISSING — re-register")
    hmac_seed = bytes(ex.hmac_seed_encrypted)
    body = await request.body()
    if not verify_hmac(hmac_seed, ts=x_hmac_timestamp, nonce=x_hmac_nonce,
                       body=body, signature_hex=x_hmac_signature):
        raise HTTPException(401, detail="HMAC_INVALID")
    store.add(x_hmac_nonce)
    return ex
