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


def check_auth_startup_config(settings) -> None:
    """Fail closed: in non-dev mode, refuse insecure auth config."""
    if settings.auth_dev_mode:
        return
    if settings.system_jwt_secret == "dev-system-jwt-change-me":
        raise RuntimeError(
            "insecure system_jwt_secret in non-dev mode; set DLW_SYSTEM_JWT_SECRET")
    if settings.oidc_issuer:
        from dlw.auth.oidc import parse_tenant_rules
        for r in parse_tenant_rules(settings.auth_tenant_rules_json):
            if r.match == "email_domain" and r.value == "*":
                raise RuntimeError(
                    "wildcard email_domain tenant rule forbidden in non-dev mode")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """W3c: leader-gated lifespan. The W3a auth substrate is bootstrapped
    unconditionally (both roles need it ready so promotion is instant). The
    run_recovery_routine + sweep loop are started by the leader loop only
    after this instance acquires the leader advisory lock."""
    from dlw.db.session import get_engine, reset_engine
    from dlw.services.leader_election import LeaderElector, run_leader_loop
    from dlw.services.recovery import run_recovery_routine

    factory = async_sessionmaker(get_engine(), expire_on_commit=False)

    # Local auth bootstrap — runs unconditionally so both active and standby
    # can serve login requests immediately after startup.
    from dlw.config import get_settings as _gs0
    _bs_settings = _gs0()
    if _bs_settings.admin_initial_password:
        from dlw.services.local_auth import bootstrap_admin
        async with factory() as _bs:
            _created = await bootstrap_admin(
                _bs, _bs_settings.admin_username,
                _bs_settings.admin_initial_password)
            if _created:
                await _bs.commit()
                logger.info("bootstrapped local admin user '%s'",
                            _bs_settings.admin_username)

    # W3a auth bootstrap — UNCHANGED. Both active and standby need this ready.
    from dlw.auth.uvicorn_tls_patch import install_transport_scope_patch
    install_transport_scope_patch()

    import secrets as _secrets
    from pathlib import Path

    from dlw.auth.ca import bootstrap_ca, ensure_server_cert
    from dlw.auth.hmac_nonce import NonceStore
    from dlw.auth.jwt_signing import bootstrap_keypair
    from dlw.config import get_settings as _gs
    _settings = _gs()
    check_auth_startup_config(_settings)
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

    # Phase 3 SP1: settings + casbin enforcer on app.state, UNCONDITIONAL
    # (both active and standby answer authz on user-plane task/quota routes;
    # require_principal/require_perm read these at request time). casbin
    # grants come from the casbin_rule table — empty in SP1, the extension
    # point for a later sub-project. ASGI tests seed these via
    # make_app_with_state (which bypasses lifespan); this is the prod path.
    app.state.settings = _settings
    from dlw.authz.enforcer import build_enforcer, load_grants
    async with factory() as _cas_session:
        _grants = await load_grants(_cas_session)
    app.state.casbin = build_enforcer(grants=_grants)

    from dlw.sources.name_resolver import NameResolver
    from dlw.sources.registry import load_registry
    app.state.source_registry = load_registry(
        _settings.sources_yaml_path, hf_token=_settings.hf_token)
    app.state.name_resolver = NameResolver.from_file(
        _settings.resolver_rules_path)

    # W3c: controller state + leader loop.
    app.state.controller_state = "standby"
    shutdown = asyncio.Event()
    elector = LeaderElector(
        db_url=_settings.db_url, lock_id=_settings.active_lock_id,
    )
    sweep_task_holder: dict[str, asyncio.Task | None] = {"t": None}
    quota_task_holder: dict[str, asyncio.Task | None] = {"t": None}
    sched_task_holder: dict[str, asyncio.Task | None] = {"t": None}
    rebalance_task_holder: dict[str, asyncio.Task | None] = {"t": None}

    async def _quota_loop() -> None:
        from dlw.services.quota import aggregate_snapshots
        while True:
            try:
                await asyncio.sleep(60)
                async with factory() as session:
                    await aggregate_snapshots(session)
                    await session.commit()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("quota aggregator tick failed; retrying")

    async def _scheduling_loop() -> None:
        from dlw.services.source_scheduler import run_scheduling_tick
        while True:
            try:
                await asyncio.sleep(5)
                async with factory() as session:
                    await run_scheduling_tick(
                        session, app.state.source_registry,
                        app.state.name_resolver, _gs())
                    await session.commit()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("scheduling tick failed; retrying")

    async def _rebalance_loop() -> None:
        from dlw.services.source_scheduler import run_rebalance_tick
        while True:
            try:
                await asyncio.sleep(_gs().rebalance_interval_seconds)
                async with factory() as session:
                    await run_rebalance_tick(session, _gs())
                    await session.commit()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("rebalance tick failed; retrying")

    gc_task_holder: dict[str, asyncio.Task | None] = {"t": None}
    physical_gc_task_holder: dict[str, asyncio.Task | None] = {"t": None}
    replication_worker_task_holder: dict[str, asyncio.Task | None] = {"t": None}
    throughput_flush_task_holder: dict[str, asyncio.Task | None] = {"t": None}
    throughput_retention_task_holder: dict[str, asyncio.Task | None] = {"t": None}
    replan_task_holder: dict[str, asyncio.Task | None] = {"t": None}

    async def _gc_loop() -> None:
        from dlw.services.audit import write_audit
        from dlw.services.storage_objects import gc_orphans
        while True:
            try:
                await asyncio.sleep(_gs().gc_interval_seconds)
                async with factory() as session:
                    n = await gc_orphans(
                        session, grace_seconds=_gs().gc_grace_seconds)
                    if n:
                        # banner 7d: GC reclaim is an audited event.
                        await write_audit(
                            session, action="storage.gc",
                            resource_type="storage_objects",
                            resource_id=None, outcome="success",
                            tenant_id=None, actor_user_id=None,
                            payload={"reclaimed": n})
                        logger.info("gc reclaimed %d storage_objects", n)
                    await session.commit()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("gc tick failed; retrying")

    async def _physical_gc_loop() -> None:
        from sqlalchemy import select as _select

        from dlw.db.models.storage import StorageBackend
        from dlw.services.audit import write_audit
        from dlw.services.storage_client import make_s3_client, storage_config_from_backend
        from dlw.services.storage_objects import pressured_tenant_ids, reclaim_physical_orphans
        while True:
            try:
                await asyncio.sleep(_gs().gc_interval_seconds)
                grace = max(_gs().gc_grace_seconds,
                            _gs().gc_archive_after_days * 86400)
                async with factory() as session:
                    backends = (await session.execute(
                        _select(StorageBackend))).scalars().all()
                    bmap = {b.id: b for b in backends}
                    cache: dict[int, tuple] = {}

                    def _make_client(sid: int):
                        if sid not in cache:
                            b = bmap.get(sid)
                            if b is None:
                                cache[sid] = (None, "", "missing")
                            else:
                                cfg = storage_config_from_backend(b)
                                client = (make_s3_client(cfg)
                                          if b.backend_type == "s3" else None)
                                cache[sid] = (client, cfg.bucket, b.backend_type)
                        return cache[sid]

                    priority = await pressured_tenant_ids(
                        session,
                        threshold=_gs().gc_quota_pressure_threshold)
                    audited: list[dict] = []
                    res = await reclaim_physical_orphans(
                        session, grace_seconds=grace,
                        delete_enabled=_gs().gc_delete_physical_bytes,
                        make_client=_make_client,
                        audit=lambda **kw: audited.append(kw),
                        max_objects_per_tick=_gs().gc_max_objects_per_tick,
                        priority_tenant_ids=priority)
                    for a in audited:
                        # resource_id is String(128); the (<=1024-char) key goes
                        # in payload — truncation here would abort the commit
                        # AFTER the S3 bytes were deleted.
                        await write_audit(
                            session, action=a["action"],
                            resource_type="storage_physical_keys",
                            resource_id=str(a["id"]), outcome="success",
                            tenant_id=a["tenant_id"], actor_user_id=None,
                            payload={"storage_key": a["storage_key"],
                                     "size": a["size"]})
                    await session.commit()
                    if res["candidates"]:
                        logger.info(
                            "physical gc: %d candidate(s), %d deleted "
                            "(delete_enabled=%s)", res["candidates"],
                            res["deleted"], _gs().gc_delete_physical_bytes)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("physical gc tick failed; retrying")

    async def _replication_worker_loop() -> None:
        from dlw.services.replication_worker import worker_loop
        s = _gs()
        if not s.replication_worker_enabled:
            logger.info("replication_worker_loop: disabled by config")
            return
        await worker_loop(
            factory,
            poll_interval_seconds=s.replication_worker_poll_interval_seconds,
            bandwidth_mbps=s.replication_bandwidth_mbps)

    async def _throughput_flush_loop_runner() -> None:
        from dlw.services.throughput_sampler import flush_loop
        s = _gs()
        if not s.throughput_sampler_enabled:
            logger.info("throughput_sampler.flush_loop: disabled by config")
            return
        await flush_loop(
            factory,
            interval_seconds=s.throughput_sampler_flush_interval_seconds)

    async def _throughput_retention_loop_runner() -> None:
        from dlw.services.throughput_sampler import retention_loop
        s = _gs()
        if not s.throughput_sampler_enabled:
            return
        await retention_loop(
            factory,
            retention_days=s.throughput_sample_retention_days)

    async def _replan_loop_runner() -> None:
        # The loop itself reads the ENABLED + APPLY env vars on each tick,
        # so a deploy can flip them without restart. We start the loop
        # unconditionally — its first action is to check the flag.
        from dlw.services.replan_loop import replan_loop
        s = _gs()
        await replan_loop(
            factory, interval_seconds=s.adaptive_optimizer_interval_seconds)

    def _set_state(s: str) -> None:
        app.state.controller_state = s

    async def _on_promote() -> None:
        async with factory() as session:
            stats = await run_recovery_routine(session)
            await session.commit()
            logger.info("recovery on promote: %s", stats.as_dict())

    async def _on_active() -> None:
        sweep_task_holder["t"] = asyncio.create_task(_sweep_loop_main(factory))
        quota_task_holder["t"] = asyncio.create_task(_quota_loop())
        sched_task_holder["t"] = asyncio.create_task(_scheduling_loop())
        rebalance_task_holder["t"] = asyncio.create_task(_rebalance_loop())
        gc_task_holder["t"] = asyncio.create_task(_gc_loop())
        physical_gc_task_holder["t"] = asyncio.create_task(_physical_gc_loop())
        replication_worker_task_holder["t"] = asyncio.create_task(
            _replication_worker_loop())
        throughput_flush_task_holder["t"] = asyncio.create_task(
            _throughput_flush_loop_runner())
        throughput_retention_task_holder["t"] = asyncio.create_task(
            _throughput_retention_loop_runner())
        replan_task_holder["t"] = asyncio.create_task(_replan_loop_runner())

    async def _on_step_down() -> None:
        t = sweep_task_holder["t"]
        if t is not None:
            t.cancel()
            try:
                await asyncio.wait_for(t, timeout=2)
            except (TimeoutError, asyncio.CancelledError):
                pass
            sweep_task_holder["t"] = None
        qt = quota_task_holder["t"]
        if qt is not None:
            qt.cancel()
            try:
                await asyncio.wait_for(qt, timeout=2)
            except (TimeoutError, asyncio.CancelledError):
                pass
            quota_task_holder["t"] = None
        t = sched_task_holder["t"]
        if t is not None:
            t.cancel()
            try:
                await asyncio.wait_for(t, timeout=2)
            except (TimeoutError, asyncio.CancelledError):
                pass
            sched_task_holder["t"] = None
        t = rebalance_task_holder["t"]
        if t is not None:
            t.cancel()
            try:
                await asyncio.wait_for(t, timeout=2)
            except (TimeoutError, asyncio.CancelledError):
                pass
            rebalance_task_holder["t"] = None
        gt = gc_task_holder["t"]
        if gt is not None:
            gt.cancel()
            try:
                await asyncio.wait_for(gt, timeout=2)
            except (TimeoutError, asyncio.CancelledError):
                pass
            gc_task_holder["t"] = None
        pt = physical_gc_task_holder["t"]
        if pt is not None:
            pt.cancel()
            try:
                await asyncio.wait_for(pt, timeout=2)
            except (TimeoutError, asyncio.CancelledError):
                pass
            physical_gc_task_holder["t"] = None
        rt = replication_worker_task_holder["t"]
        if rt is not None:
            rt.cancel()
            try:
                await asyncio.wait_for(rt, timeout=2)
            except (TimeoutError, asyncio.CancelledError):
                pass
            replication_worker_task_holder["t"] = None
        for holder in (throughput_flush_task_holder,
                        throughput_retention_task_holder,
                        replan_task_holder):
            tt = holder["t"]
            if tt is not None:
                tt.cancel()
                try:
                    await asyncio.wait_for(tt, timeout=2)
                except (TimeoutError, asyncio.CancelledError):
                    pass
                holder["t"] = None

    leader_task = asyncio.create_task(run_leader_loop(
        elector=elector,
        poll_interval_seconds=_settings.leader_poll_interval_seconds,
        set_state=_set_state,
        on_promote=_on_promote,
        on_active=_on_active,
        on_step_down=_on_step_down,
        shutdown=shutdown,
    ))
    try:
        yield
    finally:
        shutdown.set()
        await _on_step_down()
        try:
            await asyncio.wait_for(leader_task, timeout=5)
        except (TimeoutError, asyncio.CancelledError):
            leader_task.cancel()
            try:
                await leader_task
            except (asyncio.CancelledError, Exception):
                pass
        await elector.release()
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
    from dlw.api.metrics import router as metrics_router
    app.include_router(metrics_router)
    from dlw.config import get_settings as _gs2
    app.state.settings = _gs2()
    from dlw.api.auth import router as auth_router
    app.include_router(auth_router)
    from dlw.api.local_auth import router as local_auth_router
    app.include_router(local_auth_router)
    from dlw.api.tenants import router as tenants_router
    app.include_router(tenants_router)
    from dlw.api.admin_gc import router as admin_gc_router
    app.include_router(admin_gc_router)
    from dlw.api.replication import router as replication_router
    app.include_router(replication_router)
    from dlw.api.reverse_ws import router as reverse_ws_router
    app.include_router(reverse_ws_router)
    # SP5c MUST be registered BEFORE tasks_router so the static `/stream`
    # path wins over `/{task_id}` (FastAPI iterates routers in include order).
    from dlw.api.tasks_list_stream import router as tasks_list_stream_router
    app.include_router(tasks_list_stream_router)
    # SP5f: tasks-events stream router included BEFORE tasks_router for
    # defensive consistency with SP5c/SP5d/SP5e lesson. The new path
    # `/{task_id}/events/stream` has distinct depth from any existing
    # `/{task_id}/*` route, so no actual collision — pattern only.
    from dlw.api.tasks_events_stream import router as tasks_events_stream_router
    app.include_router(tasks_events_stream_router)
    # SP5g: tasks-chunks stream router included BEFORE tasks_router for
    # defensive consistency. Distinct depth from any /{task_id}/* route.
    from dlw.api.tasks_chunks_stream import router as tasks_chunks_stream_router
    app.include_router(tasks_chunks_stream_router)
    # SP5h: tasks-source-alloc stream router included BEFORE tasks_router
    # for defensive consistency. Distinct depth from any /{task_id}/* route.
    from dlw.api.tasks_source_alloc_stream import router as tasks_source_alloc_stream_router
    app.include_router(tasks_source_alloc_stream_router)
    # SP5i: tasks-executors stream router included BEFORE tasks_router for
    # defensive consistency. Distinct depth from any /{task_id}/* route.
    from dlw.api.tasks_executors_stream import router as tasks_executors_stream_router
    app.include_router(tasks_executors_stream_router)
    from dlw.api.tasks import router as tasks_router
    app.include_router(tasks_router)
    from dlw.api.executors import router as executors_router
    app.include_router(executors_router)
    from dlw.api.subtasks import router as subtasks_router
    app.include_router(subtasks_router)
    from dlw.api.hf_proxy import router as hf_proxy_router
    app.include_router(hf_proxy_router)
    # SP5e: quota stream router registered BEFORE quota_router for
    # defensive consistency with SP5c/SP5d lesson (static paths win on
    # include order; harmless if no parameterized sibling exists today).
    from dlw.api.quota_stream import router as quota_stream_router
    app.include_router(quota_stream_router)
    from dlw.api.quota import router as quota_router
    app.include_router(quota_router)
    from dlw.api.source_proxy import router as source_proxy_router
    app.include_router(source_proxy_router)
    # SP5d: audit stream router registered BEFORE audit_router for
    # defensive consistency with SP5c lesson (static paths win on include
    # order; harmless if no parameterized sibling exists today).
    from dlw.api.audit_stream import router as audit_stream_router
    app.include_router(audit_stream_router)
    from dlw.api.audit import router as audit_router
    app.include_router(audit_router)
    from dlw.api.executors_read import router as executors_read_router
    app.include_router(executors_read_router)
    from dlw.api.tasks_stream import router as tasks_stream_router
    app.include_router(tasks_stream_router)
    from dlw.api.executors_stream import router as executors_stream_router
    app.include_router(executors_stream_router)
    from dlw.api.ai import router as ai_router
    app.include_router(ai_router)
    from dlw.api.help import router as help_router
    app.include_router(help_router)

    # DX only: advertise the Bearer-JWT scheme in the generated OpenAPI so
    # Swagger /docs shows an "Authorize" button and authenticated
    # "Try it out" calls send `Authorization: Bearer <token>`. Auth itself
    # remains the manual require_principal/require_perm dependency — this
    # changes the doc, never request handling.
    from fastapi.openapi.utils import get_openapi

    def _custom_openapi() -> dict:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title, version=app.version,
            description="modelpull controller API. Authorize with a "
                        "tenant-user system-JWT (Bearer).",
            routes=app.routes,
        )
        # Name matches the static contract api/openapi.yaml (`bearerAuth`).
        schema.setdefault("components", {}).setdefault(
            "securitySchemes", {})["bearerAuth"] = {
                "type": "http", "scheme": "bearer", "bearerFormat": "JWT",
                "description": "OIDC-issued system-JWT (HS256) or "
                               "system_admin service token",
        }
        schema["security"] = [{"bearerAuth": []}]
        app.openapi_schema = schema
        return schema

    app.openapi = _custom_openapi
    return app


# uvicorn target: dlw.main:app
app = create_app()
