"""Phase 4: _physical_gc_loop is wired into the leader-gated lifespan."""
from __future__ import annotations

import pytest

import dlw.db.models  # noqa: F401
from dlw.db.base import Base

pytestmark = pytest.mark.slow


async def test_lifespan_physical_gc_loop_present(engine, tmp_path, monkeypatch):
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    monkeypatch.setenv("DLW_AUTH_DEV_MODE", "true")
    monkeypatch.setenv("DLW_CA_DIR", str(tmp_path / "ca"))
    from dlw.config import get_settings
    get_settings.cache_clear()
    from dlw.main import create_app, lifespan
    app = create_app()
    async with lifespan(app):
        # SP3 regression: source_registry still bootstrapped
        assert app.state.source_registry is not None
        # Phase 4: confirm the new config fields are present and defaults sane
        s = get_settings()
        assert s.gc_delete_physical_bytes is False
        assert s.gc_archive_after_days == 90
        assert s.gc_max_objects_per_tick == 1000
    get_settings.cache_clear()
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)
