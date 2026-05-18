"""casbin enforcer matrix — SP1 is tenant-scoped only (no project_match)."""
from __future__ import annotations

from dlw.authz.enforcer import build_enforcer


def _e():
    return build_enforcer(grants=[])


def _enforce(e, role, tenant, obj, act, rtenant):
    # request: sub, tenant, obj, act, rtenant
    return e.enforce(f"role:{role}", tenant, obj, act, rtenant)


def test_tenant_operator_can_post_tasks_same_tenant():
    assert _enforce(_e(), "tenant_operator", 1,
                    "/api/v1/tasks", "POST", 1) is True


def test_tenant_viewer_cannot_post_tasks():
    assert _enforce(_e(), "tenant_viewer", 1,
                    "/api/v1/tasks", "POST", 1) is False


def test_tenant_viewer_can_get_task_by_id():
    assert _enforce(_e(), "tenant_viewer", 1,
                    "/api/v1/tasks/abc", "GET", 1) is True


def test_cross_tenant_denied():
    assert _enforce(_e(), "tenant_operator", 1,
                    "/api/v1/tasks", "POST", 2) is False


def test_system_admin_any():
    assert _enforce(_e(), "system_admin", 1,
                    "/api/v1/anything", "DELETE", 99) is True


def test_anchored_act_regex_rejects_superstring():
    # regexMatch is unanchored (Go semantics); policy acts MUST be ^(...)$
    # so a bogus method like "POSTX" does NOT match the POST rule.
    assert _enforce(_e(), "tenant_operator", 1,
                    "/api/v1/tasks", "POSTX", 1) is False


def test_quota_get_allowed_tasks_path_not_confused():
    # keyMatch (not keyMatch2): trailing * matches to end; /api/v1/quota*
    # must NOT let a tasks-only viewer hit quota via path confusion.
    assert _enforce(_e(), "tenant_viewer", 1,
                    "/api/v1/quota/current", "GET", 1) is True
