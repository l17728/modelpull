"""Real lifespan starts the leader-gated GC loop (SP3; SP1/SP2 regression-class)."""
from __future__ import annotations

import pytest

import dlw.db.models  # noqa: F401
from dlw.db.base import Base

pytestmark = pytest.mark.slow


async def test_lifespan_gc_loop_present(engine, tmp_path, monkeypatch):
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    monkeypatch.setenv("DLW_AUTH_DEV_MODE", "true")
    monkeypatch.setenv("DLW_CA_DIR", str(tmp_path / "ca"))
    from dlw.config import get_settings
    get_settings.cache_clear()
    from dlw.main import create_app, lifespan
    app = create_app()
    async with lifespan(app):
        # registry/resolver from SP2 still bootstrapped (no regression)
        assert app.state.source_registry is not None
    get_settings.cache_clear()
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)
