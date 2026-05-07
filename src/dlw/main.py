"""FastAPI app factory + lifespan."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from dlw.api.health import router as health_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Lifespan hook — wire up shared resources here in later phases."""
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="modelpull controller",
        version="0.1.0-alpha",
        lifespan=lifespan,
    )
    app.include_router(health_router)
    return app


# uvicorn target: dlw.main:app
app = create_app()
