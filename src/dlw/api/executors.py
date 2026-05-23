"""Executors API: register / renew / heartbeat / poll.

Phase 2 W3a changes:
  - DELETE /join (W1) — replaced by /register with enrollment-token auth.
  - ADD /register: signs CSR, issues cert + JWT + hmac_seed.
  - ADD /{id}/renew: refreshes JWT; optionally re-signs cert if CSR provided.
  - /heartbeat: bearer removed; now requires require_hmac_heartbeat + require_executor_epoch.
  - /poll: bearer removed; require_executor_epoch (transitively pulls mTLS+JWT).
"""
from __future__ import annotations

import json
import logging
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.api._recovery_barrier import require_not_recovering
from dlw.api.tasks import _session
from dlw.auth import jwt_signing
from dlw.auth.ca import fingerprint_of, sign_csr
from dlw.auth.executor_epoch import require_executor_epoch
from dlw.auth.executor_jwt_dep import require_executor_jwt
from dlw.auth.hmac_heartbeat_dep import require_hmac_heartbeat
from dlw.config import get_settings
from dlw.db.models.executor import Executor
from dlw.db.models.source import SubtaskChunk
from dlw.db.models.storage import StorageBackend
from dlw.db.models.task import DownloadTask
from dlw.schemas.executor import (
    AssignmentResponse,
    ExecutorHeartbeat,
    ExecutorRead,
    ExecutorRegister,
    ReclaimItem,
    RegistrationResponse,
    RenewRequest,
    RenewResponse,
)
from dlw.schemas.storage import StorageConfig
from dlw.schemas.subtask import ChunkAssignment, SubTaskRead
from dlw.services.audit import write_audit
from dlw.services.executor_service import record_heartbeat, upsert_executor_with_cert
from dlw.services.scheduler import claim_one_subtask
from dlw.services.storage_objects import (
    confirm_local_reclaim,
    dispatch_local_reclaim,
    resolve_accessible_storage_ids,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/executors", tags=["executors"])


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def post_register(
    body: ExecutorRegister,
    request: Request,
    x_enrollment_token: str = Header(..., alias="X-Enrollment-Token"),
    session: AsyncSession = Depends(_session),
) -> RegistrationResponse:
    """W3a: enrollment-token auth; signs CSR; returns cert + JWT + hmac_seed."""
    expected = request.app.state.enrollment_token
    if not secrets.compare_digest(x_enrollment_token, expected):
        raise HTTPException(401, detail="invalid enrollment token")
    try:
        cert_pem = sign_csr(
            request.app.state.ca,
            body.client_csr_pem.encode("utf-8"),
            executor_id=body.executor_id_proposal,
            ttl_hours=24,
        )
    except ValueError as e:
        raise HTTPException(422, detail=f"invalid CSR: {e}") from e
    fp = fingerprint_of(cert_pem)
    hmac_seed = secrets.token_bytes(32)
    ex = await upsert_executor_with_cert(
        session, executor_id=body.executor_id_proposal,
        host_id=body.host_id, capabilities=body.capabilities,
        cert_fingerprint=fp, hmac_seed=hmac_seed,
    )
    token = jwt_signing.sign(
        request.app.state.jwt_keypair,
        executor_id=ex.id, epoch=ex.epoch,
        scopes=["heartbeat", "poll", "report"],
    )
    await session.commit()
    return RegistrationResponse(
        executor_id=ex.id, epoch=ex.epoch,
        client_cert_pem=cert_pem.decode("utf-8"),
        ca_chain=[request.app.state.ca.cert_pem.decode("utf-8")],
        executor_jwt=token,
        hmac_seed_hex=hmac_seed.hex(),
        cert_renew_in_seconds=86100,
        jwt_renew_in_seconds=3300,
    )


@router.post("/{executor_id}/renew")
async def post_renew(
    executor_id: str,
    body: RenewRequest,
    request: Request,
    ex: Executor = Depends(require_executor_jwt),
    session: AsyncSession = Depends(_session),
) -> RenewResponse:
    """W3a: always refresh JWT; sign a new cert iff the request carries a CSR."""
    if executor_id != ex.id:
        raise HTTPException(401, detail="path executor_id mismatch")
    new_jwt = jwt_signing.sign(
        request.app.state.jwt_keypair,
        executor_id=ex.id, epoch=ex.epoch,
        scopes=["heartbeat", "poll", "report"],
    )
    new_cert_pem: str | None = None
    new_cert_renew_in: int | None = None
    if body.client_csr_pem:
        try:
            new_cert_bytes = sign_csr(
                request.app.state.ca,
                body.client_csr_pem.encode("utf-8"),
                executor_id=ex.id, ttl_hours=24,
            )
        except ValueError as e:
            raise HTTPException(422, detail=f"invalid CSR: {e}") from e
        new_cert_pem = new_cert_bytes.decode("utf-8")
        ex.cert_fingerprint = fingerprint_of(new_cert_bytes)
        new_cert_renew_in = 86100
    await session.commit()
    return RenewResponse(
        executor_jwt=new_jwt, jwt_renew_in_seconds=3300,
        client_cert_pem=new_cert_pem,
        cert_renew_in_seconds=new_cert_renew_in,
    )


@router.post("/{executor_id}/heartbeat",
             dependencies=[Depends(require_not_recovering)])
async def post_heartbeat(
    body: ExecutorHeartbeat,
    _hmac: Executor = Depends(require_hmac_heartbeat),
    executor: Executor = Depends(require_executor_epoch),
    session: AsyncSession = Depends(_session),
) -> ExecutorRead:
    s = get_settings()
    try:
        ex = await record_heartbeat(session, executor.id, body)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    async def _audit(*, action, id, tenant_id, storage_key, size):
        await write_audit(
            session, action=action,
            resource_type="storage_physical_keys",
            resource_id=str(id), outcome="success",
            tenant_id=tenant_id, actor_user_id=None,
            payload={"storage_key": storage_key, "size": size},
        )

    # FU4: resolve accessible storage_ids from advertised base_paths (computed once,
    # passed to both confirm and dispatch so they widen in lockstep — §0 control 1).
    paths = frozenset((ex.capabilities or {}).get("base_paths") or [])
    acc_ids = frozenset(await resolve_accessible_storage_ids(session, paths))

    # FU9: adopt NULL-executor_id local keys on backends this executor mounts.
    # Gated on both acc_ids being non-empty AND executor.tenant_id being set
    # (prevents cross-tenant adoption when tenant_id is None).
    if acc_ids and executor.tenant_id is not None:
        from dlw.services.storage_objects import adopt_orphan_local_keys
        _adopted = await adopt_orphan_local_keys(
            session, executor.id,
            accessible_storage_ids=acc_ids,
            limit=s.gc_max_objects_per_tick,
            tenant_id=executor.tenant_id,
        )
        if _adopted:
            await write_audit(
                session, action="storage.local.adopt",
                resource_type="storage_physical_keys",
                resource_id=executor.id, outcome="success",
                tenant_id=executor.tenant_id, actor_user_id=None,
                payload={"adopted": _adopted},
            )

    if body.reclaimed_key_ids:
        await confirm_local_reclaim(
            session, executor.id, body.reclaimed_key_ids, audit=_audit,
            accessible_storage_ids=acc_ids, tenant_id=executor.tenant_id)

    reclaim: list[ReclaimItem] = []
    if s.gc_delete_physical_bytes:
        reclaim = await dispatch_local_reclaim(
            session, executor.id,
            grace_seconds=max(s.gc_grace_seconds, s.gc_archive_after_days * 86400),
            limit=s.gc_max_objects_per_tick,
            accessible_storage_ids=acc_ids, tenant_id=executor.tenant_id,
        )
        # Observability: count local orphan keys not dispatchable to any executor.
        try:
            from dlw.services.storage_objects import count_stuck_local_orphans
            stuck = await count_stuck_local_orphans(session)
            if stuck:
                logger.info(
                    "local-reclaim: %d stuck local orphan(s) (null executor_id or past-grace~live)",
                    stuck,
                )
        except Exception:  # noqa: BLE001
            pass

    await session.commit()
    return ExecutorRead.model_validate(ex).model_copy(update={"reclaim": reclaim})


@router.post("/{executor_id}/poll",
             dependencies=[Depends(require_not_recovering)])
async def post_poll(
    executor: Executor = Depends(require_executor_epoch),
    session: AsyncSession = Depends(_session),
) -> AssignmentResponse:
    sub, token = await claim_one_subtask(session, executor.id, executor.epoch)
    if sub is None:
        return AssignmentResponse(assigned=False)

    parent = await session.get(DownloadTask, sub.task_id)
    if parent is None:
        raise HTTPException(status_code=500, detail="parent task missing")
    storage = await session.get(StorageBackend, parent.storage_id)
    if storage is None:
        raise HTTPException(status_code=500, detail="storage backend missing")

    raw = bytes(storage.config_encrypted) if storage.config_encrypted else b"{}"
    try:
        cfg_dict = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        cfg_dict = {}
    cfg_dict.setdefault("bucket", storage.name)
    cfg_dict.setdefault("region", storage.region or "us-east-1")
    cfg_dict.setdefault("backend_type", storage.backend_type)
    storage_config = StorageConfig(**cfg_dict)

    sub_read = SubTaskRead.model_validate(sub)
    if sub.is_chunked:
        rows = (await session.execute(
            select(SubtaskChunk).where(SubtaskChunk.subtask_id == sub.id)
            .order_by(SubtaskChunk.chunk_index))).scalars().all()
        sub_read = sub_read.model_copy(update={
            "chunks": [ChunkAssignment.model_validate(c) for c in rows]})
    await session.commit()
    return AssignmentResponse(
        assigned=True,
        subtask=sub_read,
        assignment_token=token,
        repo_id=parent.repo_id,
        revision=parent.revision,
        storage_config=storage_config,
    )
