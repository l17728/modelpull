"""Regression: the REAL lifespan (not make_app_with_state) must bootstrap
app.state.casbin + app.state.settings, or every user-plane request 500s in
production. This gap shipped once because all other ASGI tests bypass
lifespan via make_app_with_state — see Phase 3 SP1 final review."""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from dlw.db.base import Base

pytestmark = pytest.mark.slow


async def test_real_lifespan_sets_casbin_and_settings(
    engine: AsyncEngine, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # load_grants() queries the casbin_rule table — schema must exist.
    import dlw.db.models  # noqa: F401  register all tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    monkeypatch.setenv("DLW_AUTH_DEV_MODE", "true")  # skip startup guard
    monkeypatch.setenv("DLW_CA_DIR", str(tmp_path / "ca"))
    from dlw.config import get_settings
    get_settings.cache_clear()

    import casbin

    from dlw.main import create_app, lifespan
    app = create_app()
    async with lifespan(app):
        assert isinstance(app.state.casbin, casbin.Enforcer)
        assert app.state.settings is not None
        # the enforcer must actually enforce (deny a viewer POST)
        assert app.state.casbin.enforce(
            "role:tenant_viewer", 1, "/api/v1/tasks", "POST", 1) is False

    get_settings.cache_clear()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
