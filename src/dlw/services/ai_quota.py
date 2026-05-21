"""AI token-budget quota (UI-SP4d, invariant 18).

A per-tenant monthly LLM token cap, independent of the download-traffic
quota in services/quota.py. Exhaustion blocks AI chat turns only.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from dlw.db.models.ai import AITokenUsage
from dlw.db.models.tenant import Tenant


class AITokenBudgetExceeded(Exception):
    """Raised when a tenant's month-to-date AI token usage meets/exceeds quota."""

    def __init__(self, remaining: int) -> None:
        super().__init__("ai token budget exceeded")
        self.remaining = remaining


def _month_start() -> datetime:
    now = datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def check_ai_token_budget(session: AsyncSession, tenant_id: int) -> int:
    """Return remaining AI tokens this calendar month. Raise
    AITokenBudgetExceeded if month-to-date usage already meets/exceeds quota."""
    tenant = await session.get(Tenant, tenant_id)
    quota = int(tenant.quota_ai_tokens_month) if tenant else 0
    used = await session.scalar(
        select(func.coalesce(
            func.sum(AITokenUsage.tokens_input + AITokenUsage.tokens_output), 0))
        .where(AITokenUsage.tenant_id == tenant_id,
               AITokenUsage.occurred_at >= _month_start()))
    remaining = quota - int(used or 0)
    if remaining <= 0:
        raise AITokenBudgetExceeded(0)
    return remaining


async def record_ai_token_usage(
    session: AsyncSession, *, tenant_id: int, user_id: int | None,
    conversation_id: uuid.UUID | None, model_name: str | None,
    tokens_input: int, tokens_output: int,
) -> None:
    """Insert one usage-ledger row. Caller commits."""
    session.add(AITokenUsage(
        tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id,
        model_name=model_name, tokens_input=tokens_input,
        tokens_output=tokens_output))
