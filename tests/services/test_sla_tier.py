"""Service tests for v2.1 SP1 SLA tier."""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.db.base import Base
from dlw.services.sla_tier import (
    ADMISSION_REJECT_BULK_ABOVE,
    ADMISSION_REJECT_STANDARD_ABOVE,
    TIER_BULK, TIER_CRITICAL, TIER_STANDARD, TIER_WEIGHTS,
    InvalidTier, TenantNotFound, TierPatch,
    admission_decision, set_tenant_sla_tier, tier_weight, validate_tier,
)


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    import dlw.db.models  # noqa: F401 — register all models
    from dlw.db.models.tenant import Tenant
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add(Tenant(id=100, slug="sla-test", display_name="SLA test"))
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def session(engine):
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        yield s


# ---------------------------------------------------------------------------
# Pure-function tier helpers
# ---------------------------------------------------------------------------

def test_valid_tiers_all_have_weights():
    """Any tier that passes validate_tier() must also have a weight."""
    for t in (TIER_CRITICAL, TIER_STANDARD, TIER_BULK):
        validate_tier(t)
        assert t in TIER_WEIGHTS


def test_tier_weights_monotone():
    assert (TIER_WEIGHTS[TIER_CRITICAL]
            > TIER_WEIGHTS[TIER_STANDARD]
            > TIER_WEIGHTS[TIER_BULK])


def test_validate_tier_rejects_unknown():
    with pytest.raises(InvalidTier):
        validate_tier("platinum")


def test_tier_weight_unknown_defaults_to_standard():
    assert tier_weight("bogus") == TIER_WEIGHTS[TIER_STANDARD]
    assert tier_weight(None) == TIER_WEIGHTS[TIER_STANDARD]


def test_tier_weight_known_returns_exact():
    assert tier_weight(TIER_CRITICAL) == 4
    assert tier_weight(TIER_BULK) == 1


# ---------------------------------------------------------------------------
# Admission control
# ---------------------------------------------------------------------------

def test_admission_critical_always_admitted():
    for busy in (0.0, 0.5, 0.9, 0.95, 0.999, 1.0):
        assert admission_decision(TIER_CRITICAL, busy) is True


def test_admission_bulk_rejected_above_threshold():
    assert admission_decision(TIER_BULK, ADMISSION_REJECT_BULK_ABOVE - 0.01) is True
    assert admission_decision(TIER_BULK, ADMISSION_REJECT_BULK_ABOVE) is False
    assert admission_decision(TIER_BULK, 0.95) is False


def test_admission_standard_rejected_only_when_nearly_full():
    assert admission_decision(TIER_STANDARD, 0.5) is True
    assert admission_decision(TIER_STANDARD, ADMISSION_REJECT_BULK_ABOVE) is True
    assert admission_decision(TIER_STANDARD, ADMISSION_REJECT_STANDARD_ABOVE) is False


def test_admission_unknown_tier_treated_as_standard():
    assert admission_decision("ghost", 0.5) is True
    assert admission_decision("ghost", 0.999) is False


# ---------------------------------------------------------------------------
# set_tenant_sla_tier — DB writes + audit
# ---------------------------------------------------------------------------

async def test_set_sla_tier_updates_field(session):
    tenant = await set_tenant_sla_tier(
        session, tenant_id=100, actor_user_id=1,
        patch=TierPatch(sla_tier=TIER_CRITICAL))
    assert tenant.sla_tier == TIER_CRITICAL
    await session.rollback()


async def test_set_sla_tier_invalid_raises(session):
    with pytest.raises(InvalidTier):
        await set_tenant_sla_tier(
            session, tenant_id=100, actor_user_id=1,
            patch=TierPatch(sla_tier="enterprise_plus"))


async def test_set_sla_tier_missing_tenant_raises(session):
    with pytest.raises(TenantNotFound):
        await set_tenant_sla_tier(
            session, tenant_id=999999, actor_user_id=1,
            patch=TierPatch(sla_tier=TIER_BULK))


async def test_no_change_skips_audit(session):
    """Setting the same value shouldn't emit an audit row — same pattern
    as set_tenant_quota."""
    from dlw.db.models.audit import AuditLog
    # First put tenant into a known state and commit so we have a baseline.
    await set_tenant_sla_tier(
        session, tenant_id=100, actor_user_id=1,
        patch=TierPatch(sla_tier=TIER_STANDARD))
    await session.commit()
    before = (await session.execute(
        select(AuditLog).where(
            AuditLog.action == "tenant.sla_tier.update",
            AuditLog.resource_id == "100"))).scalars().all()
    # Now set the same value again
    await set_tenant_sla_tier(
        session, tenant_id=100, actor_user_id=1,
        patch=TierPatch(sla_tier=TIER_STANDARD))
    await session.commit()
    after = (await session.execute(
        select(AuditLog).where(
            AuditLog.action == "tenant.sla_tier.update",
            AuditLog.resource_id == "100"))).scalars().all()
    assert len(after) == len(before)


async def test_audit_recorded_on_change(session):
    from dlw.db.models.audit import AuditLog
    # Set to a value different from current to force a change row
    await set_tenant_sla_tier(
        session, tenant_id=100, actor_user_id=7,
        patch=TierPatch(sla_tier=TIER_BULK))
    await session.commit()
    row = (await session.execute(
        select(AuditLog).where(
            AuditLog.action == "tenant.sla_tier.update",
            AuditLog.resource_id == "100").order_by(AuditLog.id.desc()).limit(1)
    )).scalar_one()
    assert row.actor_user_id == 7
    assert row.outcome == "success"
    assert row.payload.get("after") == TIER_BULK
