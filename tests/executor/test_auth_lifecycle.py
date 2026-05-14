"""Tests for dlw.executor.auth_lifecycle (Phase 2 W3a §3.11)."""
from __future__ import annotations

import datetime as _dt
import json
import secrets

import httpx
import pytest

from dlw.auth.ca import bootstrap_ca, sign_csr
from dlw.auth.jwt_signing import bootstrap_keypair, sign as jwt_sign
from dlw.executor.auth_lifecycle import AuthState, load_or_register, register


def _make_controller_transport(ca, jwt_kp, *, executor_id: str, epoch: int = 1):
    """A MockTransport that emulates POST /register: signs the CSR, returns
    cert + JWT + hmac_seed."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/executors/register":
            payload = json.loads(request.content)
            csr_pem = payload["client_csr_pem"].encode("utf-8")
            cert_pem = sign_csr(ca, csr_pem, executor_id, ttl_hours=24)
            token = jwt_sign(jwt_kp, executor_id=executor_id, epoch=epoch,
                             scopes=["heartbeat", "poll", "report"])
            return httpx.Response(201, json={
                "executor_id": executor_id, "epoch": epoch,
                "client_cert_pem": cert_pem.decode("utf-8"),
                "ca_chain": [ca.cert_pem.decode("utf-8")],
                "executor_jwt": token,
                "hmac_seed_hex": secrets.token_bytes(32).hex(),
                "cert_renew_in_seconds": 86100,
                "jwt_renew_in_seconds": 3300,
            })
        return httpx.Response(404)
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_register_persists_state_and_parses_expiry(tmp_path, monkeypatch) -> None:
    ca = bootstrap_ca(tmp_path / "ca")
    jwt_kp = bootstrap_keypair(tmp_path / "ca")
    transport = _make_controller_transport(ca, jwt_kp, executor_id="reg-worker-1")
    # Patch httpx.AsyncClient so auth_lifecycle's internal client uses our transport.
    import dlw.executor.auth_lifecycle as al
    orig = httpx.AsyncClient
    def patched(*args, **kwargs):
        kwargs.pop("verify", None)
        kwargs.pop("cert", None)
        return orig(*args, transport=transport, **kwargs)
    monkeypatch.setattr(al.httpx, "AsyncClient", patched)

    cert_dir = tmp_path / "executor"
    state = await register(
        controller_url="http://controller", ca_bundle_path=None,
        enrollment_token="tok", executor_id="reg-worker-1",
        host_id="reg-host", capabilities={}, cert_dir=cert_dir,
    )
    assert isinstance(state, AuthState)
    assert state.executor_id == "reg-worker-1"
    assert state.epoch == 1
    assert state.jwt
    assert state.jwt_exp > _dt.datetime.now(_dt.UTC)
    assert state.cert_exp > _dt.datetime.now(_dt.UTC)
    assert len(state.hmac_seed) == 32
    # Persisted to disk
    assert (cert_dir / "client-cert.pem").exists()
    assert (cert_dir / "hmac-seed").exists()


@pytest.mark.asyncio
async def test_load_or_register_first_run_calls_register(tmp_path, monkeypatch) -> None:
    ca = bootstrap_ca(tmp_path / "ca")
    jwt_kp = bootstrap_keypair(tmp_path / "ca")
    transport = _make_controller_transport(ca, jwt_kp, executor_id="lor-worker-1")
    import dlw.executor.auth_lifecycle as al
    orig = httpx.AsyncClient
    def patched(*args, **kwargs):
        kwargs.pop("verify", None)
        kwargs.pop("cert", None)
        return orig(*args, transport=transport, **kwargs)
    monkeypatch.setattr(al.httpx, "AsyncClient", patched)

    cert_dir = tmp_path / "executor"   # empty — first run
    state = await load_or_register(
        cert_dir=cert_dir, controller_url="http://controller",
        ca_bundle_path=None, enrollment_token="tok",
        executor_id="lor-worker-1", host_id="lor-host", capabilities={},
    )
    assert state.executor_id == "lor-worker-1"
    assert (cert_dir / "client-cert.pem").exists()
