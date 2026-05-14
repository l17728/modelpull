"""Executor auth lifecycle: register / renew / load (Phase 2 W3a §3.11)."""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from pathlib import Path

import httpx
import jwt as _pyjwt
from cryptography import x509

from dlw.executor import cert as _cert


@dataclass
class AuthState:
    executor_id: str
    epoch: int
    cert_pem: bytes
    key_pem: bytes
    ca_chain_pem: bytes
    jwt: str
    jwt_exp: _dt.datetime
    cert_exp: _dt.datetime
    hmac_seed: bytes
    cert_dir: Path


def _parse_jwt_exp(token: str) -> _dt.datetime:
    claims = _pyjwt.decode(token, options={"verify_signature": False})
    return _dt.datetime.fromtimestamp(claims["exp"], tz=_dt.UTC)


def _parse_cert_exp(cert_pem: bytes) -> _dt.datetime:
    return x509.load_pem_x509_certificate(cert_pem).not_valid_after_utc


async def register(*, controller_url: str, ca_bundle_path: str | None,
                   enrollment_token: str, executor_id: str, host_id: str,
                   capabilities: dict, cert_dir: Path) -> AuthState:
    csr_pem, key_pem = _cert.build_csr(executor_id)
    verify = ca_bundle_path if ca_bundle_path else True
    async with httpx.AsyncClient(verify=verify) as c:
        r = await c.post(f"{controller_url}/api/v1/executors/register", json={
            "host_id": host_id, "executor_id_proposal": executor_id,
            "capabilities": capabilities,
            "client_csr_pem": csr_pem.decode("utf-8"),
        }, headers={"X-Enrollment-Token": enrollment_token})
        r.raise_for_status()
    body = r.json()
    cert_pem = body["client_cert_pem"].encode("utf-8")
    ca_chain_pem = "\n".join(body["ca_chain"]).encode("utf-8")
    hmac_seed = bytes.fromhex(body["hmac_seed_hex"])
    _cert.persist(cert_dir, cert_pem=cert_pem, key_pem=key_pem,
                  ca_chain_pem=ca_chain_pem, hmac_seed=hmac_seed)
    return AuthState(
        executor_id=body["executor_id"], epoch=body["epoch"],
        cert_pem=cert_pem, key_pem=key_pem, ca_chain_pem=ca_chain_pem,
        jwt=body["executor_jwt"], jwt_exp=_parse_jwt_exp(body["executor_jwt"]),
        cert_exp=_parse_cert_exp(cert_pem), hmac_seed=hmac_seed,
        cert_dir=cert_dir,
    )


async def renew(state: AuthState, *, controller_url: str) -> AuthState:
    """POST /{eid}/renew over mTLS. Include a fresh CSR iff cert TTL < 1h."""
    now = _dt.datetime.now(_dt.UTC)
    payload: dict = {}
    new_key_pem = state.key_pem
    if state.cert_exp - now < _dt.timedelta(hours=1):
        csr_pem, new_key_pem = _cert.build_csr(state.executor_id)
        payload["client_csr_pem"] = csr_pem.decode("utf-8")
    cert_file = state.cert_dir / "client-cert.pem"
    key_file = state.cert_dir / "client-key.pem"
    async with httpx.AsyncClient(
        verify=str(state.cert_dir / "ca-chain.pem"),
        cert=(str(cert_file), str(key_file)),
        headers={"Authorization": f"Bearer {state.jwt}"},
    ) as c:
        r = await c.post(
            f"{controller_url}/api/v1/executors/{state.executor_id}/renew",
            json=payload,
        )
        r.raise_for_status()
    body = r.json()
    new_jwt = body["executor_jwt"]
    cert_pem = state.cert_pem
    cert_exp = state.cert_exp
    if body.get("client_cert_pem"):
        cert_pem = body["client_cert_pem"].encode("utf-8")
        cert_exp = _parse_cert_exp(cert_pem)
        _cert.persist(state.cert_dir, cert_pem=cert_pem, key_pem=new_key_pem,
                      ca_chain_pem=state.ca_chain_pem, hmac_seed=state.hmac_seed)
    return AuthState(
        executor_id=state.executor_id, epoch=state.epoch,
        cert_pem=cert_pem, key_pem=new_key_pem, ca_chain_pem=state.ca_chain_pem,
        jwt=new_jwt, jwt_exp=_parse_jwt_exp(new_jwt), cert_exp=cert_exp,
        hmac_seed=state.hmac_seed, cert_dir=state.cert_dir,
    )


async def load_or_register(*, cert_dir: Path, controller_url: str,
                           ca_bundle_path: str | None, enrollment_token: str,
                           executor_id: str, host_id: str,
                           capabilities: dict) -> AuthState:
    """W3a simplification: always re-register on startup. The JWT is never
    persisted, so a "load + renew" path would still need a valid JWT. Re-register
    is idempotent (epoch bumps — correct W1 fence semantics). The renew loop
    handles in-process refresh; restart goes through register."""
    return await register(
        controller_url=controller_url, ca_bundle_path=ca_bundle_path,
        enrollment_token=enrollment_token, executor_id=executor_id,
        host_id=host_id, capabilities=capabilities, cert_dir=cert_dir,
    )
