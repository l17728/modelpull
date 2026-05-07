"""FastAPI app factory + lifespan."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from dlw.api.health import router as health_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Lifespan hook — dispose shared engine on shutdown.

    Engine is created lazily by get_engine() (lru_cached singleton); we only
    need to clean it up on shutdown.
    """
    yield
    from dlw.db.session import reset_engine
    await reset_engine()


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
    return app


# uvicorn target: dlw.main:app
app = create_app()
