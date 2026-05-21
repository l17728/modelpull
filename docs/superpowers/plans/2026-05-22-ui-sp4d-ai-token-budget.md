# UI-SP4d — AI Copilot Token-Budget Quota Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce a per-tenant monthly LLM token budget (invariant 18): when exhausted, AI chat turns are blocked (downloads unaffected); each turn records token usage.

**Architecture:** A new `ai_token_usage` ledger table + a `tenants.quota_ai_tokens_month` column. A `services/ai_quota.py` module provides `check_ai_token_budget` (month-to-date sum vs quota) and `record_ai_token_usage`. `run_chat` gains a pre-turn gate (emit `quota_exceeded` + return before any conversation is created) and post-turn usage recording (stub estimates ≈ chars/4 so the budget is exercisable). Frontend `useCopilot` surfaces the `quota_exceeded` event.

**Tech Stack:** FastAPI, SQLAlchemy 2 async, alembic, asyncpg; Vue 3.5 `<script setup>` + Vitest; pytest.

**Spec:** `docs/superpowers/specs/2026-05-22-ui-sp4d-ai-token-budget-design.md` (read fully — honest scope note, migration alter-table server_default rule, soft-budget race).

**Locked constraints (do NOT violate):**
- Migration `down_revision = "a2b3c4d5e6f7"` (current single head). Migrations live in `src/dlw/alembic/versions/`. `alembic.ini` at repo root; run `-c alembic.ini`.
- `ai_token_usage.id` = BIGSERIAL (`autoincrement=True`), NOT uuid. NO server_default on `tokens_input/output` (always caller-supplied). `occurred_at` server_default `now()` matching the model.
- `tenants.quota_ai_tokens_month`: this ALTERs an existing populated table → the migration column NEEDS `server_default="1000000"` to backfill, AND the model column MUST declare the SAME `server_default="1000000"` (plus `default=1_000_000`) to avoid `compare_server_default=True` drift. (This is the deliberate exception to the SP4a/4b "Python-default-only" rule — those added columns to brand-new tables.)
- Register `AITokenUsage` in `src/dlw/db/models/__init__.py` (import + `__all__`). `src/dlw/alembic/env.py` already imports `dlw.db.models`, so the `__init__` import suffices for autogenerate/metadata.
- `tests/db/test_alembic.py::EXPECTED_TABLES` += `"ai_token_usage"`.
- NO `api/openapi.yaml` change (no new HTTP surface). NO literal `null` example values anywhere (SP4a spectral CI lesson).
- Pre-turn check goes at the VERY TOP of `run_chat`, before the `async with session_maker() as s:` conversation block — an over-budget request must create NO conversation, persist NO message, invoke NO runner.
- Post-turn usage recording is BEST-EFFORT/isolated (try/except, own session) — a recording failure must not lose the turn (SP4a CRITICAL-2 pattern).
- `run_confirmation` (phase 2) is unchanged — it calls no LLM.
- Use a tenant-user JWT in API tests (not the system-admin service token: `user_id=0` → owner-FK 500).
- Both `en-US.json` / `zh-CN.json` at exact key parity.
- Existing CI gates only: pytest, spectral, swagger-cli, lint_invariants, frontend eslint `--max-warnings=0` + vue-tsc + `vitest run`, frontend-build. No new runtime deps.

---

## File Structure

- **Create** `src/dlw/alembic/versions/b3c4d5e6f7a8_p3sp4d_ai_token_budget.py` — migration: add `tenants.quota_ai_tokens_month`, create `ai_token_usage` + index.
- **Modify** `src/dlw/db/models/ai.py` — add `AITokenUsage` model.
- **Modify** `src/dlw/db/models/tenant.py` — add `Tenant.quota_ai_tokens_month`.
- **Modify** `src/dlw/db/models/__init__.py` — register `AITokenUsage`.
- **Create** `src/dlw/services/ai_quota.py` — `AITokenBudgetExceeded`, `check_ai_token_budget`, `record_ai_token_usage`.
- **Modify** `src/dlw/ai/service.py` — pre-turn gate + post-turn recording in `run_chat`.
- **Modify** `tests/db/test_alembic.py` — EXPECTED_TABLES.
- **Create** `tests/services/test_ai_quota.py` — service-level tests.
- **Modify** `tests/api/test_ai_chat.py` — over/under-budget chat tests.
- **Modify** `frontend/src/composables/useCopilot.ts` — handle `quota_exceeded`.
- **Modify** `frontend/src/locales/en-US.json` + `zh-CN.json` — `copilot.quotaExceeded`.
- **Modify** `frontend/src/composables/__tests__/useCopilot.spec.ts` — quota_exceeded test.
- **Modify** `docs/operator/web-ui.md` — SP4d section.

---

## Milestone M1 — Backend

### Task 1: `AITokenUsage` model + `Tenant.quota_ai_tokens_month`

**Files:**
- Modify: `src/dlw/db/models/ai.py`
- Modify: `src/dlw/db/models/tenant.py`
- Modify: `src/dlw/db/models/__init__.py`

- [ ] **Step 1: Add the model.** In `src/dlw/db/models/ai.py`, after `AIToolCall`, append:

```python
class AITokenUsage(Base):
    """UI-SP4d: per-turn LLM token ledger for invariant-18 budget enforcement."""
    __tablename__ = "ai_token_usage"
    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tenants.id"), nullable=False)
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tokens_input: Mapped[int] = mapped_column(Integer, nullable=False)
    tokens_output: Mapped[int] = mapped_column(Integer, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
```

(All imported names — `BigInteger`, `ForeignKey`, `Integer`, `String`, `DateTime`, `func`, `UUID`, `uuid`, `datetime`, `Mapped`, `mapped_column`, `Base` — are already imported at the top of `ai.py`. Do NOT add `Index`; the index is migration-only, consistent with `idx_ai_tool_conv`.)

- [ ] **Step 2: Add the tenant column.** In `src/dlw/db/models/tenant.py`, in `class Tenant`, after the `quota_storage_gb` line, add:

```python
    quota_ai_tokens_month: Mapped[int] = mapped_column(
        BigInteger, default=1_000_000, server_default="1000000", nullable=False)
```

(NOTE the `server_default="1000000"` — required to match the alter-table migration and avoid `compare_server_default` drift. This differs from the sibling `quota_*` columns, which have only `default=` because they were created with the table.)

- [ ] **Step 3: Register the model.** In `src/dlw/db/models/__init__.py`:
  - change the ai import line to: `from dlw.db.models.ai import AIConversation, AIMessage, AIToolCall, AITokenUsage`
  - add `"AITokenUsage",` to `__all__` (alongside the other AI names).

- [ ] **Step 4: Verify import + metadata registration.**

Run: `uv run python -c "from dlw.db.models import AITokenUsage; from dlw.db.base import Base; assert 'ai_token_usage' in Base.metadata.tables; print('ok')"`
Expected: `ok`

- [ ] **Step 5: Commit.**

```bash
git add src/dlw/db/models/ai.py src/dlw/db/models/tenant.py src/dlw/db/models/__init__.py
git commit -m "feat(sp4d): AITokenUsage model + tenants.quota_ai_tokens_month"
```

### Task 2: Migration

**Files:**
- Create: `src/dlw/alembic/versions/b3c4d5e6f7a8_p3sp4d_ai_token_budget.py`
- Modify: `tests/db/test_alembic.py:52-74` (EXPECTED_TABLES)

- [ ] **Step 1: Add EXPECTED_TABLES entry (failing test first).** In `tests/db/test_alembic.py`, add `"ai_token_usage",` to the `EXPECTED_TABLES` set (alphabetically, after `"ai_tool_calls",`).

- [ ] **Step 2: Run the alembic table test to verify it FAILS** (migration not written yet, so head won't produce the table).

Run: `uv run pytest tests/db/test_alembic.py::test_upgrade_head_creates_all_tables -v`
Expected: FAIL — XOR diff shows `{'ai_token_usage'}` missing.

- [ ] **Step 3: Write the migration.** Create `src/dlw/alembic/versions/b3c4d5e6f7a8_p3sp4d_ai_token_budget.py`:

```python
"""p3sp4d ai token budget

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
Create Date: 2026-05-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b3c4d5e6f7a8"
down_revision: str | None = "a2b3c4d5e6f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("quota_ai_tokens_month", sa.BigInteger(),
                  nullable=False, server_default="1000000"))
    op.create_table(
        "ai_token_usage",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.BigInteger(),
                  sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("user_id", sa.BigInteger(),
                  sa.ForeignKey("users.id"), nullable=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True),
                  nullable=True),
        sa.Column("model_name", sa.String(64), nullable=True),
        sa.Column("tokens_input", sa.Integer(), nullable=False),
        sa.Column("tokens_output", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_ai_usage_tenant_time", "ai_token_usage",
                    ["tenant_id", "occurred_at"])


def downgrade() -> None:
    op.drop_index("idx_ai_usage_tenant_time", table_name="ai_token_usage")
    op.drop_table("ai_token_usage")
    op.drop_column("tenants", "quota_ai_tokens_month")
```

- [ ] **Step 4: Apply to the dev DB (smoke).**

Run: `uv run alembic -c alembic.ini upgrade head`
Expected: completes; `quota_ai_tokens_month` + `ai_token_usage` created. (Dev DB is local PG 18 on :5433, trust auth.)

- [ ] **Step 5: Run the alembic tests to verify they PASS.**

Run: `uv run pytest tests/db/test_alembic.py -v`
Expected: PASS (upgrade-head produces the expected set incl. `ai_token_usage`; downgrade→base→re-upgrade clean — confirms `drop_column` reverses the alter).

- [ ] **Step 6: Verify no autogenerate drift** (the model server_default must match the migration).

Run: `uv run alembic -c alembic.ini revision --autogenerate -m _drift_check 2>&1 | Select-String "add_column|drop_column|alter_column|server_default"`
Expected: no matches relating to `quota_ai_tokens_month` / `ai_token_usage`. Then DELETE the generated drift-check file (do not commit it).

- [ ] **Step 7: Commit.**

```bash
git add src/dlw/alembic/versions/b3c4d5e6f7a8_p3sp4d_ai_token_budget.py tests/db/test_alembic.py
git commit -m "feat(sp4d): migration for ai_token_usage + quota_ai_tokens_month"
```

### Task 3: `services/ai_quota.py`

**Files:**
- Create: `src/dlw/services/ai_quota.py`
- Test: `tests/services/test_ai_quota.py`

- [ ] **Step 1: Write the failing tests.** Create `tests/services/test_ai_quota.py`:

```python
"""AI token-budget service (UI-SP4d, invariant 18)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.db.base import Base
from dlw.db.models.ai import AITokenUsage
from dlw.services.ai_quota import (AITokenBudgetExceeded,
                                   check_ai_token_budget,
                                   record_ai_token_usage)


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
    # last-month usage doesn't count → full quota remains
    assert await check_ai_token_budget(session, 2) == 1000


async def test_tenant_isolation(session):
    # tenant-1 usage already > quota (prior test in module added 1100); a fresh
    # tenant unaffected. Use tenant-2 which only has last-month usage.
    assert await check_ai_token_budget(session, 2) == 1000
```

(NOTE: module-scoped DB so tests share state; the budget tests are written to be order-independent within their own assertions. `conftest`'s `engine` fixture is the standard project async engine — same one `test_write_tools.py` uses.)

- [ ] **Step 2: Run to verify FAIL.**

Run: `uv run pytest tests/services/test_ai_quota.py -v`
Expected: FAIL — `ModuleNotFoundError: dlw.services.ai_quota`.

- [ ] **Step 3: Write the service.** Create `src/dlw/services/ai_quota.py`:

```python
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
```

- [ ] **Step 4: Run to verify PASS.**

Run: `uv run pytest tests/services/test_ai_quota.py -v`
Expected: PASS (all 5).

- [ ] **Step 5: Commit.**

```bash
git add src/dlw/services/ai_quota.py tests/services/test_ai_quota.py
git commit -m "feat(sp4d): ai_quota service — check + record token budget"
```

### Task 4: `run_chat` integration (pre-turn gate + post-turn recording)

**Files:**
- Modify: `src/dlw/ai/service.py:31-60` (add pre-turn gate), `:132-145` (post-turn tokens + recording)
- Test: `tests/api/test_ai_chat.py`

- [ ] **Step 1: Write the failing API tests.** In `tests/api/test_ai_chat.py`, add (match the file's existing app/principal/SSE-parse helpers — reuse them; do NOT invent new fixtures):

```python
async def test_chat_over_budget_blocks(client, app_state, tenant_user_headers):
    # Seed usage >= the tenant's quota.
    from dlw.db.models.ai import AITokenUsage, AIConversation
    from sqlalchemy import select, func
    async with app_state.session_maker() as s:
        s.add(AITokenUsage(
            tenant_id=TENANT_ID, user_id=USER_ID, conversation_id=None,
            model_name="stub", tokens_input=10**9, tokens_output=0))
        await s.commit()
    events = await sse_post(client, tenant_user_headers,
                            {"message": "hello"})
    assert events[0]["event"] == "quota_exceeded"
    assert events[0]["data"]["metric"] == "ai_tokens"
    # No conversation created, no assistant message.
    async with app_state.session_maker() as s:
        n = await s.scalar(select(func.count()).select_from(AIConversation))
    assert n == 0


async def test_chat_under_budget_records_usage(client, app_state,
                                               tenant_user_headers):
    from dlw.db.models.ai import AITokenUsage, AIMessage
    from sqlalchemy import select, func
    events = await sse_post(client, tenant_user_headers, {"message": "hello"})
    assert events[-1]["event"] == "done"
    async with app_state.session_maker() as s:
        rows = (await s.execute(
            select(AITokenUsage).where(
                AITokenUsage.tenant_id == TENANT_ID))).scalars().all()
        assert len(rows) == 1
        assert rows[0].tokens_input > 0
        # assistant message carries the same counts
        am = (await s.execute(
            select(AIMessage).where(AIMessage.role == "assistant"))
        ).scalars().first()
        assert am.tokens_input == rows[0].tokens_input
        assert am.tokens_output == rows[0].tokens_output
```

(Adapt the symbol names — `client`, `app_state`, `tenant_user_headers`, `sse_post`, `TENANT_ID`, `USER_ID` — to whatever `tests/api/test_ai_chat.py` already defines. Read that file first and reuse its real fixtures/helpers; the test BODIES above are the spec, the plumbing must match the file. Each test must run against a clean conversations table — follow the file's existing isolation approach.)

- [ ] **Step 2: Run to verify FAIL.**

Run: `uv run pytest tests/api/test_ai_chat.py -k "budget" -v`
Expected: FAIL — no `quota_exceeded` event; usage not recorded.

- [ ] **Step 3: Add the pre-turn gate.** In `src/dlw/ai/service.py`, add the import near the top (with the other `from dlw.services...` import):

```python
from dlw.services.ai_quota import (AITokenBudgetExceeded,
                                   check_ai_token_budget,
                                   record_ai_token_usage)
```

Then INSERT, as the very first statements inside `run_chat` (before `# 1. Resolve / create the conversation`):

```python
    # SP4d: pre-turn budget gate (invariant 18). Over budget → block the AI
    # call entirely: emit quota_exceeded and return WITHOUT creating a
    # conversation, persisting a message, or invoking the runner.
    async with session_maker() as q:
        try:
            await check_ai_token_budget(q, principal.tenant_id)
        except AITokenBudgetExceeded as exc:
            yield AgentEvent("quota_exceeded",
                             {"metric": "ai_tokens", "remaining": exc.remaining})
            return
```

- [ ] **Step 4: Add post-turn token estimate + recording.** In `run_chat` section 4, replace the assistant-message persist block so it computes nominal tokens, stamps them on the `AIMessage`, and records usage. Change:

```python
            am = AIMessage(
                conversation_id=conv_id, role="assistant",
                content={"text": "".join(assistant_text),
                         "tool_calls": tool_calls})
            s.add(am)
            await s.commit()
            ai_message_id = str(am.id)
        yield AgentEvent("done", {"conversation_id": str(conv_id),
                                  "ai_message_id": ai_message_id,
                                  "tokens_used": 0})
```

to:

```python
            text_out = "".join(assistant_text)
            # SP4d: nominal token estimate (≈ chars/4). A real backend would
            # report actual counts here; the stub consumes no real tokens, so
            # this keeps usage accumulating and the budget exercisable.
            tin = max(1, len(message) // 4)
            tout = max(0, len(text_out) // 4)
            am = AIMessage(
                conversation_id=conv_id, role="assistant",
                content={"text": text_out, "tool_calls": tool_calls},
                tokens_input=tin, tokens_output=tout)
            s.add(am)
            await s.commit()
            ai_message_id = str(am.id)
        # SP4d: best-effort/isolated usage recording (must not lose the turn).
        try:
            async with session_maker() as u:
                await record_ai_token_usage(
                    u, tenant_id=principal.tenant_id,
                    user_id=principal.user_id, conversation_id=conv_id,
                    model_name=runner.model_name,
                    tokens_input=tin, tokens_output=tout)
                await u.commit()
        except Exception:  # noqa: BLE001 — usage recording is best-effort
            pass
        yield AgentEvent("done", {"conversation_id": str(conv_id),
                                  "ai_message_id": ai_message_id,
                                  "tokens_used": tin + tout})
```

- [ ] **Step 5: Run the budget tests to verify PASS.**

Run: `uv run pytest tests/api/test_ai_chat.py -k "budget" -v`
Expected: PASS.

- [ ] **Step 6: Run the FULL ai_chat suite (regression — `done.tokens_used` changed from 0).**

Run: `uv run pytest tests/api/test_ai_chat.py -v`
Expected: PASS. If a pre-existing test asserts `tokens_used == 0`, update it to `> 0` (or the exact estimate) — that field is now real.

- [ ] **Step 7: Commit.**

```bash
git add src/dlw/ai/service.py tests/api/test_ai_chat.py
git commit -m "feat(sp4d): run_chat pre-turn budget gate + post-turn usage recording"
```

### Task 5: M1 full backend gate + dev-DB upgrade

- [ ] **Step 1: Full backend gate.**

Run: `uv run pytest -q` then `uv run ruff check src tests` then `uv run python scripts/lint_invariants.py` (use whatever the project's invariant-lint entrypoint is — match prior SPs).
Expected: all green. Fix any regression before proceeding.

- [ ] **Step 2: Confirm dev DB at head.**

Run: `uv run alembic -c alembic.ini current`
Expected: `b3c4d5e6f7a8 (head)`.

- [ ] **Step 3: No commit** (gate-only; nothing changed). Proceed to M2.

---

## Milestone M2 — Frontend

### Task 6: `useCopilot` handles `quota_exceeded`

**Files:**
- Modify: `frontend/src/composables/useCopilot.ts:72-76` (event switch in `send`)
- Modify: `frontend/src/locales/en-US.json`, `frontend/src/locales/zh-CN.json`
- Test: `frontend/src/composables/__tests__/useCopilot.spec.ts`

- [ ] **Step 1: Add i18n keys.** In `frontend/src/locales/en-US.json`, under the existing `copilot` object, add:

```json
    "quotaExceeded": "AI token budget exhausted for this month. Chat is paused until your quota resets."
```

In `frontend/src/locales/zh-CN.json`, the same key under `copilot`:

```json
    "quotaExceeded": "本月 AI token 配额已用尽，聊天暂停，等待配额重置。"
```

(Keep exact key parity. Place the key consistently within the `copilot` block in both files.)

- [ ] **Step 2: Write the failing test.** In `frontend/src/composables/__tests__/useCopilot.spec.ts`, add a test that drives a `quota_exceeded` event through the mocked `streamChat` and asserts the assistant bubble shows the budget note and streaming ends. Follow the file's existing `vi.hoisted` mock of `@/api/aiClient` (the mock's `streamChat` calls `onEvent` with scripted events). Example body (adapt to the file's existing mock harness):

```typescript
it('surfaces quota_exceeded as a budget note and stops streaming', async () => {
  streamChatMock.mockImplementation(async ({ onEvent }) => {
    onEvent({ event: 'quota_exceeded',
              data: { metric: 'ai_tokens', remaining: 0 } })
  })
  const c = useCopilot()
  await c.send('hello')
  const last = c.messages.value.at(-1)!
  expect(last.role).toBe('assistant')
  expect(last.text).toContain('budget')   // i18n note appended
  expect(c.streaming.value).toBe(false)
})
```

(NOTE: `useCopilot` does not currently import i18n. To avoid coupling the composable to vue-i18n, append a stable English-ish marker is NOT acceptable for i18n parity. Instead: emit a structured flag the component renders. SIMPLEST consistent choice — see Step 3.)

- [ ] **Step 2b: Resolve the i18n seam (decision).** `useCopilot` builds plain `text` strings (the `error` branch already appends an English `[error: ...]` literal). To stay consistent with that existing pattern AND keep i18n parity, the composable will append a sentinel the UI localizes. BUT the existing `error` branch sets a raw string, and tests assert on substrings. To minimize risk and match the established pattern, append a localized string via the i18n global instance is over-engineering for one line. **Chosen approach:** add a `quotaExceeded` boolean flag to `ChatMessage`, set it on the event, and let the assistant bubble render `t('copilot.quotaExceeded')` when the flag is set. This keeps the localized text in the Vue layer (where i18n lives) and the composable state-only. Update the test to assert `last.quotaExceeded === true` instead of a text substring.

Revised test body:

```typescript
it('surfaces quota_exceeded as a budget flag and stops streaming', async () => {
  streamChatMock.mockImplementation(async ({ onEvent }) => {
    onEvent({ event: 'quota_exceeded',
              data: { metric: 'ai_tokens', remaining: 0 } })
  })
  const c = useCopilot()
  await c.send('hello')
  const last = c.messages.value.at(-1)!
  expect(last.role).toBe('assistant')
  expect(last.quotaExceeded).toBe(true)
  expect(c.streaming.value).toBe(false)
})
```

- [ ] **Step 3: Run to verify FAIL.**

Run: `cd frontend; pnpm vitest run src/composables/__tests__/useCopilot.spec.ts`
Expected: FAIL — `quotaExceeded` undefined / branch missing.

- [ ] **Step 4: Implement.** In `useCopilot.ts`:
  - Add `quotaExceeded?: boolean` to the `ChatMessage` interface.
  - In `send`'s `onEvent` switch, add a branch BEFORE the `done`/`error` branches:

```typescript
          } else if (ev.event === 'quota_exceeded') {
            assistant.quotaExceeded = true
```

- [ ] **Step 5: Render the note in the drawer.** In `CopilotDrawer.vue` (the assistant bubble render), where the assistant `text` is shown, add — when `message.quotaExceeded` — a localized note element, e.g.:

```vue
<el-alert
  v-if="message.quotaExceeded"
  :title="t('copilot.quotaExceeded')"
  type="warning" :closable="false" show-icon />
```

(Match the component's existing i18n usage — it already calls `useI18n()`/`t`. Place the alert within the assistant message block. Verify `el-alert` is available via the global ElementPlus plugin — it is, same as other `el-*` already used.)

- [ ] **Step 6: Run to verify PASS + drawer spec regression.**

Run: `cd frontend; pnpm vitest run src/composables/__tests__/useCopilot.spec.ts src/components/copilot`
Expected: PASS. If `CopilotDrawer.spec` mocks `useCopilot` and the new `el-alert` needs no new mock field, it stays green; if the spec asserts rendered structure, update its `useCopilot` mock to include messages without `quotaExceeded` (defaults undefined — no change needed).

- [ ] **Step 7: Commit.**

```bash
git add frontend/src/composables/useCopilot.ts frontend/src/components frontend/src/locales/en-US.json frontend/src/locales/zh-CN.json frontend/src/composables/__tests__/useCopilot.spec.ts
git commit -m "feat(sp4d): copilot surfaces quota_exceeded budget note"
```

### Task 7: M2 full frontend gate

- [ ] **Step 1: Full frontend gate.**

Run: `cd frontend; pnpm lint; pnpm vue-tsc --noEmit; pnpm vitest run; pnpm build`
Expected: all green (eslint `--max-warnings=0`, vue-tsc clean, all vitest pass, build succeeds). Fix regressions before proceeding.

- [ ] **Step 2: Verify locale parity.**

Run: `cd frontend; node -e "const a=require('./src/locales/en-US.json'),b=require('./src/locales/zh-CN.json');const ka=JSON.stringify(Object.keys(a.copilot).sort()),kb=JSON.stringify(Object.keys(b.copilot).sort());if(ka!==kb){console.error('PARITY MISMATCH',ka,kb);process.exit(1)}console.log('parity ok')"`
Expected: `parity ok`.

---

## Milestone M3 — Smoke + docs

### Task 8: Manual over-budget smoke

- [ ] **Step 1: Seed over-budget usage + curl** (against the local dev API, using a tenant-user JWT — NOT the system-admin service token). Start the app, then:
  - Insert an `ai_token_usage` row with `tokens_input` ≥ the tenant's `quota_ai_tokens_month` (psql on :5433).
  - `curl -N -H "Authorization: Bearer <tenant-user-jwt>" -H "Content-Type: application/json" -d '{"message":"hi"}' http://localhost:8000/api/v1/ai/chat`
  - Expected first SSE event: `event: quota_exceeded` with `data: {"metric":"ai_tokens","remaining":0}`; no conversation row created.
  - Then delete the seed row and repeat → a normal `done` with `tokens_used > 0` and one `ai_token_usage` row.

If the dev app/JWT isn't readily available, state that explicitly in the report rather than claiming success (per project rule on UI/feature testing honesty).

### Task 9: Docs

**Files:**
- Modify: `docs/operator/web-ui.md`

- [ ] **Step 1: Append an SP4d section** documenting: the `quota_ai_tokens_month` tenant column (default 1,000,000), the monthly reset semantics (calendar month, UTC), that exhaustion blocks AI chat but NOT downloads, the `quota_exceeded` SSE event, and the honest note that the stub records a nominal estimate (real backends report actuals). No code, just operator-facing prose consistent with the file's existing style.

- [ ] **Step 2: Commit.**

```bash
git add docs/operator/web-ui.md
git commit -m "docs(sp4d): operator notes for AI token budget"
```

---

## Self-Review

**1. Spec coverage:**
- §1.1 migration → Task 2 ✓; §1.2 service → Task 3 ✓; §1.3 run_chat pre/post → Task 4 ✓; §1.4 frontend → Task 6 ✓.
- §2 data model (BIGSERIAL id, no token server_default, alter-table server_default + matching model) → Task 1 + 2 ✓.
- §3 service signatures → Task 3 ✓; §4 run_chat integration code → Task 4 ✓; §5 frontend → Task 6 ✓; §6 tests → Tasks 3,4,6 ✓; §7 milestones → M1/M2/M3 ✓.

**2. Placeholder scan:** Task 4 Step 1 and Task 6 Step 2 intentionally say "adapt to the file's existing fixtures/mock harness" — these are NOT placeholders for missing logic; the test BODIES (assertions) are fully specified, only the fixture plumbing must match the real test file, which the implementer must read. Flagged explicitly so the reviewer doesn't mistake them for gaps.

**3. Type consistency:** `check_ai_token_budget(session, tenant_id) -> int`, `record_ai_token_usage(session, *, tenant_id, user_id, conversation_id, model_name, tokens_input, tokens_output)`, `AITokenBudgetExceeded(remaining)`, `quota_exceeded {metric, remaining}`, `ChatMessage.quotaExceeded?: boolean` — used identically across tasks.

**Open risk for reviewers:** (a) the i18n seam in Task 6 (flag-in-composable + localize-in-component) — is this the most consistent choice given the existing `[error: ...]` literal pattern? (b) the alter-table server_default rule — confirm the model+migration pair is drift-free. (c) test isolation in `tests/api/test_ai_chat.py` — the over-budget test asserts zero conversations, so it must run against a clean table; confirm the file's fixture provides per-test DB reset.
