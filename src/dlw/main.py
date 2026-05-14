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

_SWEEP_INTERVAL_SECONDS = 30


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

    # W3a: bootstrap CA + JWT signing key + server cert + nonce store + enrollment token.
    from pathlib import Path
    from dlw.auth.ca import bootstrap_ca, ensure_server_cert
    from dlw.auth.jwt_signing import bootstrap_keypair
    from dlw.auth.hmac_nonce import NonceStore
    import secrets as _secrets
    from dlw.config import get_settings as _gs
    _settings = _gs()
    _ca_dir = Path(_settings.ca_dir)
    _ca_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    _ca = bootstrap_ca(_ca_dir)
    ensure_server_cert(_ca, _ca_dir, hostname=_settings.controller_hostname)
    _jwt_kp = bootstrap_keypair(_ca_dir)
    if _settings.enrollment_token:
        _enroll = _settings.enrollment_token
    else:
        _tok_path = _ca_dir / "enrollment.token"
        if _tok_path.exists():
            _enroll = _tok_path.read_text().strip()
        else:
            _enroll = _secrets.token_hex(32)
            _tok_path.write_text(_enroll)
            _tok_path.chmod(0o600)
            logger.info("generated enrollment token (copy to executors): %s", _enroll)
    app.state.ca = _ca
    app.state.jwt_keypair = _jwt_kp
    app.state.nonce_store = NonceStore(maxsize=10_000, ttl_seconds=300)
    app.state.enrollment_token = _enroll

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

    sweep_task = asyncio.create_task(_sweep_loop_main(factory))

    try:
        yield
    finally:
        sweep_task.cancel()
        try:
            await asyncio.wait_for(sweep_task, timeout=2)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        await reset_engine()


async def _sweep_loop_main(factory) -> None:
    """Background task: every N seconds, transition stale executors + reclaim
    + recover paused_disk_full subtasks."""
    from dlw.services.recovery import (
        sweep_executor_timeouts,
        sweep_paused_disk_full,
        sweep_paused_external,
    )

    while True:
        try:
            await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)
            async with factory() as session:
                await sweep_executor_timeouts(session)
                await sweep_paused_disk_full(session)
                await sweep_paused_external(session)
                await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("sweep_loop iteration failed; will retry next tick")


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
