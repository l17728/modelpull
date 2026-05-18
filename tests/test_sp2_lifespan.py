"""Real lifespan bootstraps source_registry + name_resolver (SP2)."""
from __future__ import annotations

import pytest

import dlw.db.models  # noqa: F401
from dlw.db.base import Base

pytestmark = pytest.mark.slow


async def test_lifespan_sets_source_state(engine, tmp_path, monkeypatch):
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    monkeypatch.setenv("DLW_AUTH_DEV_MODE", "true")
    monkeypatch.setenv("DLW_CA_DIR", str(tmp_path / "ca"))
    from dlw.config import get_settings
    get_settings.cache_clear()
    from dlw.main import create_app, lifespan
    from dlw.sources.registry import SourceRegistry
    app = create_app()
    async with lifespan(app):
        assert isinstance(app.state.source_registry, SourceRegistry)
        assert app.state.name_resolver is not None
        assert "huggingface" in app.state.source_registry.enabled_ids()
    get_settings.cache_clear()
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)
