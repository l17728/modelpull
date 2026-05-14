"""require_executor_epoch — W1 fence-token dep, refactored for W3a §3.4.

Under W3a it chains under require_executor_jwt: the Executor row is already
loaded + authenticated via the mTLS cert fingerprint. This dep adds:
  1. path executor_id MUST equal the mTLS-authenticated identity
     (confused-deputy guard);
  2. X-Executor-Epoch MUST match the authenticated row's epoch (W1 fence).
"""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Path

from dlw.auth.executor_jwt_dep import require_executor_jwt
from dlw.db.models.executor import Executor


async def require_executor_epoch(
    executor_id: str = Path(..., description="Executor id from URL path"),
    x_executor_epoch: int | None = Header(default=None, alias="X-Executor-Epoch"),
    ex: Executor = Depends(require_executor_jwt),
) -> Executor:
    """Return the mTLS+JWT-authenticated Executor row if path id matches and
    epoch header matches; else 401."""
    if executor_id != ex.id:
        raise HTTPException(
            status_code=401,
            detail={"code": "EXECUTOR_ID_MISMATCH",
                    "path": executor_id, "authenticated": ex.id},
        )
    if x_executor_epoch is None:
        raise HTTPException(status_code=401, detail="missing X-Executor-Epoch header")
    if ex.epoch != x_executor_epoch:
        raise HTTPException(
            status_code=401,
            detail={"code": "EPOCH_MISMATCH", "expected": ex.epoch,
                    "got": x_executor_epoch},
        )
    return ex
