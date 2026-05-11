"""FastAPI app factory + lifespan."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.api.health import router as health_router

logger = logging.getLogger(__name__)

_RECLAIM_INTERVAL_SECONDS = 30


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Phase 2 W1: run recovery_routine before serving + spawn reclaim_loop.

    Order:
      1. Recovery routine (synchronous; must complete before serving traffic)
      2. Spawn background reclaim_loop task
      3. yield (app serves traffic)
      4. Cancel reclaim_loop + dispose engine on shutdown
    """
    from dlw.db.session import get_engine, reset_engine
    from dlw.services.recovery import run_recovery_routine

    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    # W6-J: spec §7 says recovery failure aborts startup. Permissive dev mode
    # via DLW_STRICT_RECOVERY=false env override (defaults to strict).
    import os
    strict_recovery = os.environ.get("DLW_STRICT_RECOVERY", "true").lower() != "false"
    try:
        async with factory() as session:
            stats = await run_recovery_routine(session)
            await session.commit()    # W6-E: caller commits (service is no-commit)
            logger.info("startup recovery: %s", stats.as_dict())
    except Exception:
        if strict_recovery:
            logger.exception("startup recovery_routine failed; aborting startup (strict mode)")
            raise
        logger.exception(
            "startup recovery_routine failed; continuing in permissive mode "
            "(DLW_STRICT_RECOVERY=false)"
        )

    reclaim_task = asyncio.create_task(_reclaim_loop_main(factory))

    try:
        yield
    finally:
        reclaim_task.cancel()
        try:
            await asyncio.wait_for(reclaim_task, timeout=2)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        await reset_engine()


async def _reclaim_loop_main(factory) -> None:
    """Background task: every N seconds, scan stale executors + reclaim."""
    from dlw.services.recovery import reclaim_stale_executors

    while True:
        try:
            await asyncio.sleep(_RECLAIM_INTERVAL_SECONDS)
            async with factory() as session:
                await reclaim_stale_executors(session)
                await session.commit()       # W6-E: caller commits
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("reclaim_loop iteration failed; will retry next tick")


def create_app() -> FastAPI:
    app = FastAPI(
        title="modelpull controller",
        version="0.1.0-alpha",
        lifespan=lifespan,
    )
    app.include_router(health_router)
    from dlw.api.tasks import router as tasks_router
    app.include_router(tasks_router)
    from dlw.api.executors import router as executors_router
    app.include_router(executors_router)
    from dlw.api.subtasks import router as subtasks_router
    app.include_router(subtasks_router)
    return app


# uvicorn target: dlw.main:app
app = create_app()
