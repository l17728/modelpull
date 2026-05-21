"""AI token-budget service (UI-SP4d, invariant 18)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.db.base import Base
from dlw.db.models.ai import AITokenUsage
from dlw.services.ai_quota import (
    AITokenBudgetExceeded,
    check_ai_token_budget,
    record_ai_token_usage,
)


@pytest.fixture(scope="module", autouse=True)
async def _bootstrap(engine):
    from dlw.db.models.tenant import Tenant
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        s.add(Tenant(id=1, slug="t1", display_name="T1",
                     quota_ai_tokens_month=1000))
        s.add(Tenant(id=2, slug="t2", display_name="T2",
                     quota_ai_tokens_month=1000))
        await s.commit()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def session(engine):
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as s:
        yield s


async def test_no_usage_returns_full_quota(session):
    assert await check_ai_token_budget(session, 1) == 1000


async def test_partial_usage_returns_remaining(session):
    await record_ai_token_usage(
        session, tenant_id=1, user_id=1, conversation_id=None,
        model_name="stub", tokens_input=300, tokens_output=100)
    await session.commit()
    assert await check_ai_token_budget(session, 1) == 600


async def test_over_budget_raises(session):
    await record_ai_token_usage(
        session, tenant_id=1, user_id=1, conversation_id=None,
        model_name="stub", tokens_input=900, tokens_output=200)
    await session.commit()
    with pytest.raises(AITokenBudgetExceeded) as ei:
        await check_ai_token_budget(session, 1)
    assert ei.value.remaining == 0


async def test_last_month_usage_excluded(session):
    last_month = datetime.now(UTC).replace(day=1) - timedelta(days=2)
    session.add(AITokenUsage(
        tenant_id=2, user_id=2, conversation_id=None, model_name="stub",
        tokens_input=900, tokens_output=200, occurred_at=last_month))
    await session.commit()
    assert await check_ai_token_budget(session, 2) == 1000


async def test_tenant_isolation(session):
    assert await check_ai_token_budget(session, 2) == 1000
