"""Principal / system-JWT decode tests (Phase 3 SP1)."""
from __future__ import annotations

import time
import types

import jwt as _pyjwt
import pytest
from fastapi import HTTPException

from dlw.auth.principal import (
    Principal,
    issue_system_jwt,
    require_principal,
)

SECRET = "unit-secret"


def _req(svc_token: str = ""):
    """Minimal stand-in for fastapi.Request exposing app.state.settings."""
    settings = types.SimpleNamespace(
        system_jwt_secret=SECRET, system_admin_token=svc_token
    )
    app = types.SimpleNamespace(state=types.SimpleNamespace(settings=settings))
    return types.SimpleNamespace(app=app)


async def test_issue_and_decode_roundtrip():
    tok = issue_system_jwt(secret=SECRET, user_id=7, tenant_id=3,
                           role="tenant_operator", project_ids=[1, 2])
    p = await require_principal(_req(), authorization=f"Bearer {tok}")
    assert p == Principal(user_id=7, tenant_id=3, role="tenant_operator",
                          project_ids=(1, 2), is_service=False)


async def test_missing_header_401():
    with pytest.raises(HTTPException) as e:
        await require_principal(_req(), authorization=None)
    assert e.value.status_code == 401


async def test_expired_token_401():
    tok = issue_system_jwt(secret=SECRET, user_id=1, tenant_id=1,
                           role="tenant_viewer", project_ids=[], ttl_seconds=-10)
    with pytest.raises(HTTPException) as e:
        await require_principal(_req(), authorization=f"Bearer {tok}")
    assert e.value.status_code == 401


async def test_forged_signature_401():
    tok = _pyjwt.encode(
        {"iss": "dlw-controller", "sub": "1", "tid": 1, "role": "x",
         "pids": [], "iat": int(time.time()), "exp": int(time.time()) + 60},
        "wrong-secret", algorithm="HS256")
    with pytest.raises(HTTPException) as e:
        await require_principal(_req(), authorization=f"Bearer {tok}")
    assert e.value.status_code == 401


async def test_wrong_issuer_401():
    tok = _pyjwt.encode(
        {"iss": "evil", "sub": "1", "tid": 1, "role": "x", "pids": [],
         "iat": int(time.time()), "exp": int(time.time()) + 60},
        SECRET, algorithm="HS256")
    with pytest.raises(HTTPException) as e:
        await require_principal(_req(), authorization=f"Bearer {tok}")
    assert e.value.status_code == 401


async def test_service_token_yields_service_principal():
    p = await require_principal(_req(svc_token="svc-xyz"),
                                authorization="Bearer svc-xyz")
    assert p.is_service is True
    assert p.role == "system_admin"
    assert p.tenant_id == 1
    assert p.user_id == 0
