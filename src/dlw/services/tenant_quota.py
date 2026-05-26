"""Set tenant quota limits — system_admin only.

Updates the quota_* columns on the tenants table. Writes an audit log
entry recording the before/after values so quota changes are traceable.
Caller commits (services.audit.write_audit also runs inside the same
transaction so a single commit covers both)."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.tenant import Tenant
from dlw.services.audit import write_audit

logger = logging.getLogger(__name__)


class TenantNotFound(LookupError):
    """No tenant with that id."""


class InvalidQuota(ValueError):
    """Quota value out of range."""


@dataclass(frozen=True)
class TenantQuotaPatch:
    """Optional fields. None means "leave unchanged"."""
    quota_bytes_month: int | None = None
    quota_concurrent: int | None = None
    quota_storage_gb: int | None = None
    quota_ai_tokens_month: int | None = None


def _validate(name: str, value: int) -> None:
    if value < 0:
        logger.warning("set_tenant_quota: rejected negative %s=%d", name, value)
        raise InvalidQuota(f"{name} must be >= 0")
    # Upper bound is generous (10 PB / 10k concurrent) to catch obvious typos
    # without restricting realistic deployments.
    if value > 10 * 1024 * 1024 * 1024 * 1024 * 1024:
        logger.warning("set_tenant_quota: rejected oversized %s=%d", name, value)
        raise InvalidQuota(f"{name} too large")


async def set_tenant_quota(
    session: AsyncSession, *, tenant_id: int, patch: TenantQuotaPatch,
    actor_user_id: int,
) -> Tenant:
    """Apply patch to the named tenant. Raises TenantNotFound / InvalidQuota.
    Writes an audit log entry. Returns the mutated tenant (uncommitted)."""
    tenant = await session.get(Tenant, int(tenant_id))
    if tenant is None:
        logger.warning(
            "set_tenant_quota: tenant %d not found (actor=%d)",
            tenant_id, actor_user_id)
        raise TenantNotFound(str(tenant_id))

    before = {
        "quota_bytes_month": tenant.quota_bytes_month,
        "quota_concurrent": tenant.quota_concurrent,
        "quota_storage_gb": tenant.quota_storage_gb,
        "quota_ai_tokens_month": tenant.quota_ai_tokens_month,
    }
    changes: dict[str, tuple[int, int]] = {}
    for field in ("quota_bytes_month", "quota_concurrent",
                  "quota_storage_gb", "quota_ai_tokens_month"):
        new = getattr(patch, field)
        if new is None:
            continue
        new_int = int(new)
        _validate(field, new_int)
        old = before[field]
        if old != new_int:
            setattr(tenant, field, new_int)
            changes[field] = (old, new_int)

    if changes:
        logger.info(
            "set_tenant_quota: tenant=%d actor=%d changes=%s",
            tenant_id, actor_user_id,
            {k: {"before": v[0], "after": v[1]} for k, v in changes.items()})
        await write_audit(
            session, action="tenant.quota.update",
            resource_type="tenant", resource_id=str(tenant_id),
            outcome="success", tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            payload={"changes": {k: {"before": v[0], "after": v[1]}
                                  for k, v in changes.items()}})
    else:
        logger.debug(
            "set_tenant_quota: tenant=%d no-op (all values unchanged)",
            tenant_id)
    await session.flush()
    return tenant
