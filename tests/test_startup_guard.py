"""Startup guard refuses insecure prod config (Phase 3 SP1)."""
from __future__ import annotations

import pytest

from dlw.main import check_auth_startup_config


def _s(**kw):
    import types
    base = dict(auth_dev_mode=False,
                system_jwt_secret="dev-system-jwt-change-me",
                oidc_issuer="", auth_tenant_rules_json="[]")
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_dev_mode_allows_anything():
    check_auth_startup_config(_s(auth_dev_mode=True))  # no raise


def test_prod_insecure_jwt_secret_refused():
    with pytest.raises(RuntimeError, match="system_jwt_secret"):
        check_auth_startup_config(_s(system_jwt_secret="dev-system-jwt-change-me",
                                     oidc_issuer="https://idp"))


def test_prod_missing_issuer_refused():
    with pytest.raises(RuntimeError, match="oidc_issuer"):
        check_auth_startup_config(_s(system_jwt_secret="strong", oidc_issuer=""))


def test_prod_wildcard_rule_refused():
    with pytest.raises(RuntimeError, match="wildcard"):
        check_auth_startup_config(_s(
            system_jwt_secret="strong", oidc_issuer="https://idp",
            auth_tenant_rules_json='[{"match":"email_domain","value":"*",'
            '"tenant_slug":"default","role":"tenant_operator"}]'))


def test_prod_valid_config_ok():
    check_auth_startup_config(_s(
        system_jwt_secret="strong", oidc_issuer="https://idp",
        auth_tenant_rules_json='[{"match":"group","value":"g",'
        '"tenant_slug":"t","role":"tenant_viewer"}]'))
