"""OIDC dev-mode + tenant-resolution tests (Phase 3 SP1)."""
from __future__ import annotations

import pytest

from dlw.auth.oidc import (
    OidcClaims,
    TenantRule,
    exchange_code_dev,
    resolve_tenant,
)


def test_dev_exchange_parses_email_from_code():
    claims = exchange_code_dev("dev:alice@acme.com")
    assert claims == OidcClaims(sub="dev:alice@acme.com",
                                email="alice@acme.com", groups=())


def test_dev_exchange_rejects_non_dev_code():
    with pytest.raises(ValueError):
        exchange_code_dev("realcode123")


def test_resolve_tenant_email_domain_rule():
    rules = [TenantRule(match="email_domain", value="acme.com",
                        tenant_slug="acme", role="tenant_operator")]
    slug, role = resolve_tenant(
        OidcClaims(sub="s", email="bob@acme.com", groups=()), rules)
    assert (slug, role) == ("acme", "tenant_operator")


def test_resolve_tenant_group_rule():
    rules = [TenantRule(match="group", value="ml-eng",
                        tenant_slug="research", role="project_member")]
    slug, role = resolve_tenant(
        OidcClaims(sub="s", email="x@y.z", groups=("ml-eng",)), rules)
    assert (slug, role) == ("research", "project_member")


def test_resolve_tenant_no_match_returns_none():
    rules = [TenantRule(match="email_domain", value="acme.com",
                        tenant_slug="acme", role="tenant_viewer")]
    assert resolve_tenant(
        OidcClaims(sub="s", email="x@other.com", groups=()), rules) is None


def test_resolve_tenant_wildcard_rule():
    rules = [TenantRule(match="email_domain", value="*",
                        tenant_slug="default", role="tenant_operator")]
    slug, role = resolve_tenant(
        OidcClaims(sub="s", email="anyone@anywhere", groups=()), rules)
    assert slug == "default"


def test_parse_rules_from_json():
    from dlw.auth.oidc import parse_tenant_rules
    rules = parse_tenant_rules(
        '[{"match":"group","value":"g","tenant_slug":"t","role":"tenant_viewer"}]')
    assert rules == [TenantRule(match="group", value="g",
                                tenant_slug="t", role="tenant_viewer")]
