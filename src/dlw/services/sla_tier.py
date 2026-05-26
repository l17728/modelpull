"""v2.1 Sprint 1 — SLA tier service.

Tier definitions used by the scheduler + quota admission control.
Wiring to scheduler.py and quota.py is deferred to Sprint 1 implementation
PRs; this module owns the constants + getters + setter so callers can
import a stable surface from day one."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.tenant import Tenant
from dlw.services.audit import write_audit

logger = logging.getLogger(__name__)

# Valid tiers (high → low priority).
TIER_CRITICAL = "critical"
TIER_STANDARD = "standard"
TIER_BULK = "bulk"
VALID_TIERS: frozenset[str] = frozenset({TIER_CRITICAL, TIER_STANDARD, TIER_BULK})

# Scheduler weights — multiply task.priority for ordering.
# Doubling between tiers is enough to dominate intra-tier priority noise
# (priority is 0-10) without making bulk completely starve.
TIER_WEIGHTS: dict[str, int] = {
    TIER_CRITICAL: 4,
    TIER_STANDARD: 2,
    TIER_BULK: 1,
}

# Starvation guard: bulk tasks that wait this long get bumped to standard
# weight for scheduling purposes (still recorded as bulk in DB).
BULK_STARVATION_TIMEOUT_SECONDS = 30 * 60   # 30 min

# Admission control thresholds. system_busy_fraction = active / max_concurrent.
ADMISSION_REJECT_BULK_ABOVE = 0.90       # > 90% busy: reject new bulk
ADMISSION_REJECT_STANDARD_ABOVE = 0.99   # > 99% busy: reject new standard too


class InvalidTier(ValueError):
    """Raised when an unrecognized tier value is supplied."""


class TenantNotFound(LookupError):
    """Raised when a tenant id doesn't exist."""


@dataclass(frozen=True)
class TierPatch:
    sla_tier: str


def validate_tier(tier: str) -> None:
    if tier not in VALID_TIERS:
        raise InvalidTier(
            f"sla_tier must be one of {sorted(VALID_TIERS)}, got {tier!r}")


def tier_weight(tier: str | None) -> int:
    """Return the scheduler weight for a tier. Unknown / None defaults to
    standard so a typo or missing value doesn't break scheduling."""
    if tier and tier in TIER_WEIGHTS:
        return TIER_WEIGHTS[tier]
    return TIER_WEIGHTS[TIER_STANDARD]


def admission_decision(tier: str, system_busy_fraction: float) -> bool:
    """Return True if a new task at this tier should be admitted now.

    Critical is always admitted. Standard is rejected only when the system
    is essentially full (> 99%). Bulk is rejected above 90% to leave
    headroom for critical/standard."""
    if tier == TIER_CRITICAL:
        return True
    if tier == TIER_STANDARD:
        return system_busy_fraction < ADMISSION_REJECT_STANDARD_ABOVE
    if tier == TIER_BULK:
        return system_busy_fraction < ADMISSION_REJECT_BULK_ABOVE
    # Unknown tier: treat as standard.
    return system_busy_fraction < ADMISSION_REJECT_STANDARD_ABOVE


async def set_tenant_sla_tier(
    session: AsyncSession, *, tenant_id: int, patch: TierPatch,
    actor_user_id: int,
) -> Tenant:
    """Update a tenant's sla_tier. Audit-logged on change. Caller commits."""
    validate_tier(patch.sla_tier)
    tenant = await session.get(Tenant, int(tenant_id))
    if tenant is None:
        logger.warning(
            "set_tenant_sla_tier: tenant %d not found (actor=%d)",
            tenant_id, actor_user_id)
        raise TenantNotFound(str(tenant_id))
    old = tenant.sla_tier
    if old == patch.sla_tier:
        logger.debug(
            "set_tenant_sla_tier: tenant=%d no-op (already %s)",
            tenant_id, old)
        return tenant
    tenant.sla_tier = patch.sla_tier
    logger.info(
        "set_tenant_sla_tier: tenant=%d actor=%d before=%s after=%s",
        tenant_id, actor_user_id, old, patch.sla_tier)
    await write_audit(
        session, action="tenant.sla_tier.update",
        resource_type="tenant", resource_id=str(tenant_id),
        outcome="success", tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        payload={"before": old, "after": patch.sla_tier})
    await session.flush()
    return tenant
