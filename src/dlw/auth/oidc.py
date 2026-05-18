"""OIDC authorization-code exchange + tenant resolution (Phase 3 SP1).

Real mode uses Authlib against settings.oidc_issuer. Dev mode
(settings.auth_dev_mode) skips the network: the `code` is `dev:<email>`
and claims are synthesized — keeps CI hermetic (no live IdP), the same
philosophy as the Phase 2 enrollment-token / local-PG-no-Docker setup."""
from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class OidcClaims:
    sub: str
    email: str
    groups: tuple[str, ...]


@dataclass(frozen=True)
class TenantRule:
    match: str        # "email_domain" | "group"
    value: str        # domain (or "*") | group name
    tenant_slug: str
    role: str


def parse_tenant_rules(raw: str) -> list[TenantRule]:
    data = json.loads(raw or "[]")
    return [
        TenantRule(
            match=d["match"], value=d["value"],
            tenant_slug=d["tenant_slug"], role=d["role"],
        )
        for d in data
    ]


def exchange_code_dev(code: str) -> OidcClaims:
    """Dev-mode code is 'dev:<email>'. Raises ValueError otherwise."""
    if not code.startswith("dev:"):
        raise ValueError("dev mode expects a 'dev:<email>' code")
    email = code.removeprefix("dev:")
    return OidcClaims(sub=code, email=email, groups=())


async def exchange_code_real(
    *, code: str, state: str, issuer: str, client_id: str,
    client_secret: str, redirect_url: str,
) -> OidcClaims:
    """Real OIDC: code->token, verify id_token via JWKS, return claims."""
    import httpx
    from authlib.integrations.httpx_client import AsyncOAuth2Client
    from authlib.jose import JsonWebToken

    async with httpx.AsyncClient(timeout=10) as http:
        meta = (await http.get(
            f"{issuer.rstrip('/')}/.well-known/openid-configuration")).json()
        oauth = AsyncOAuth2Client(
            client_id, client_secret, redirect_uri=redirect_url)
        token = await oauth.fetch_token(
            meta["token_endpoint"], code=code, state=state,
            grant_type="authorization_code")
        jwks = (await http.get(meta["jwks_uri"])).json()
    id_tok = token["id_token"]
    claims = JsonWebToken(["RS256", "ES256", "EdDSA"]).decode(
        id_tok, jwks)
    claims.validate()
    grp = claims.get("groups") or []
    return OidcClaims(
        sub=str(claims["sub"]),
        email=str(claims.get("email", "")),
        groups=tuple(grp),
    )


def resolve_tenant(
    claims: OidcClaims, rules: list[TenantRule]
) -> tuple[str, str] | None:
    """First matching rule wins. Returns (tenant_slug, role) or None."""
    domain = claims.email.split("@")[-1] if "@" in claims.email else ""
    for r in rules:
        if r.match == "email_domain" and r.value in ("*", domain):
            return r.tenant_slug, r.role
        if r.match == "group" and r.value in claims.groups:
            return r.tenant_slug, r.role
    return None
