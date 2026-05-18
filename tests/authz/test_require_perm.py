"""require_perm dependency: allow / deny (tenant-scoped only, SP1)."""
from __future__ import annotations

import types

import pytest
from fastapi import HTTPException

from dlw.auth.principal import Principal
from dlw.authz.deps import require_perm


class _FakeSession:
    def add(self, *_): ...
    async def commit(self): ...


def _req():
    from dlw.authz.enforcer import build_enforcer
    app = types.SimpleNamespace(state=types.SimpleNamespace(
        casbin=build_enforcer(grants=[])))
    return types.SimpleNamespace(app=app)


async def test_allows_operator_same_tenant():
    dep = require_perm("/api/v1/tasks*", "POST")
    p = Principal(user_id=1, tenant_id=1, role="tenant_operator",
                  project_ids=())
    out = await dep(request=_req(), principal=p, session=_FakeSession())
    assert out is p


async def test_denies_viewer_post():
    dep = require_perm("/api/v1/tasks*", "POST")
    p = Principal(user_id=1, tenant_id=1, role="tenant_viewer",
                  project_ids=())
    with pytest.raises(HTTPException) as e:
        await dep(request=_req(), principal=p, session=_FakeSession())
    assert e.value.status_code == 403
    assert e.value.detail["code"] == "RBAC_DENIED"


async def test_viewer_can_get():
    dep = require_perm("/api/v1/tasks*", "GET")
    p = Principal(user_id=1, tenant_id=1, role="tenant_viewer",
                  project_ids=())
    out = await dep(request=_req(), principal=p, session=_FakeSession())
    assert out is p


async def test_service_principal_short_circuits():
    dep = require_perm("/api/v1/tasks*", "DELETE")
    p = Principal(user_id=0, tenant_id=1, role="system_admin",
                  project_ids=(), is_service=True)
    out = await dep(request=_req(), principal=p, session=_FakeSession())
    assert out is p
