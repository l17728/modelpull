"""Health endpoints: /health/live (process up?) + /health/ready (db reachable?)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.db.session import get_engine

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def live() -> dict[str, str]:
    """Liveness — always 200 if process responding (used by k8s livenessProbe)."""
    return {"status": "healthy"}


@router.get("/ready")
async def ready() -> dict[str, str]:
    """Readiness — DB-dependent (used by k8s readinessProbe + LB).

    Engine is shared (lru_cached); disposing per-request would race with
    concurrent probes under realistic k8s 1s intervals.
    """
    engine = get_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"db unreachable: {exc}") from exc
    return {"status": "ready", "db": "ok"}
