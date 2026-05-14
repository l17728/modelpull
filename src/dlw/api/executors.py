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
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.api.tasks import _session
from dlw.auth.ca import fingerprint_of, sign_csr
from dlw.auth import jwt_signing
from dlw.auth.executor_jwt_dep import require_executor_jwt
from dlw.auth.hmac_heartbeat_dep import require_hmac_heartbeat
from dlw.auth.executor_epoch import require_executor_epoch
from dlw.db.models.executor import Executor
from dlw.db.models.storage import StorageBackend
from dlw.db.models.task import DownloadTask
from dlw.schemas.executor import (
    AssignmentResponse,
    ExecutorHeartbeat,
    ExecutorRead,
    ExecutorRegister,
    RegistrationResponse,
    RenewRequest,
    RenewResponse,
)
from dlw.schemas.storage import StorageConfig
from dlw.schemas.subtask import SubTaskRead
from dlw.services.executor_service import upsert_executor_with_cert, record_heartbeat
from dlw.services.scheduler import claim_one_subtask

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


@router.post("/{executor_id}/heartbeat")
async def post_heartbeat(
    body: ExecutorHeartbeat,
    _hmac: Executor = Depends(require_hmac_heartbeat),
    executor: Executor = Depends(require_executor_epoch),
    session: AsyncSession = Depends(_session),
) -> ExecutorRead:
    try:
        ex = await record_heartbeat(session, executor.id, body)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    await session.commit()
    return ExecutorRead.model_validate(ex)


@router.post("/{executor_id}/poll")
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
    storage_config = StorageConfig(**cfg_dict)

    sub_read = SubTaskRead.model_validate(sub)
    await session.commit()
    return AssignmentResponse(
        assigned=True,
        subtask=sub_read,
        assignment_token=token,
        repo_id=parent.repo_id,
        revision=parent.revision,
        storage_config=storage_config,
    )
