# UI-SP4d — AI Copilot Token-Budget Quota (Design)

> Fourth slice of the v2.1 AI Copilot (after SP4a read-only, SP4b write+confirm).
> Delivers 🔒 **invariant 18**: per-tenant LLM token quota, isolated from
> download-traffic quota — when exhausted, AI calls are blocked but
> downloads are unaffected.
> Status: design self-approved per Rule #1 (named SP4 deferred slice).
> Branch: `feat/ui-sp4d-ai-token-budget`.
>
> **Honest scope note**: the CI-tested path uses the deterministic `stub`
> backend, which consumes no *real* LLM tokens. SP4d therefore (a) builds
> the enforcement machinery that protects a real-LLM backend from runaway
> spend, and (b) makes it exercisable by having `run_chat` record a
> **nominal** token estimate per turn (≈ chars/4) so usage accumulates and
> the budget is testable with seeded usage. Real backends (opencode / a
> future Anthropic runner) would report actual token counts here.

## 1. Scope

**In scope (additive, 1 migration):**

1. **Migration**: `tenants.quota_ai_tokens_month` (BigInteger, default
   `1_000_000`, NOT NULL) + new `ai_token_usage` table (per doc §5).
   Chains off `a2b3c4d5e6f7`.
2. **Service** (`src/dlw/services/ai_quota.py`):
   - `AITokenBudgetExceeded(remaining: int)` exception.
   - `check_ai_token_budget(session, tenant_id) -> int` — returns
     remaining tokens this calendar month; raises if `used >= quota`.
   - `record_ai_token_usage(session, *, tenant_id, user_id,
     conversation_id, model_name, tokens_input, tokens_output)` —
     inserts an `ai_token_usage` row (caller commits/flushes).
3. **`run_chat` integration**:
   - **Pre-turn** (very top, before conversation create): call
     `check_ai_token_budget`; on `AITokenBudgetExceeded` emit a
     `quota_exceeded` event `{metric:"ai_tokens", remaining}` + return —
     **no conversation created, no runner invoked** (inv 18: block the
     AI call).
   - **Post-turn** (after the assistant message persists): estimate
     `tokens_input ≈ len(message)//4`, `tokens_output ≈
     len(assistant_text)//4` (≥1 each if non-empty); record an
     `ai_token_usage` row AND set those on the assistant `ai_messages`
     row. Best-effort/isolated (a usage-record failure must not lose the
     turn — same SP4a CRITICAL-2 pattern).
   - Phase-2 confirmation (`run_confirmation`) does NOT call the LLM, so
     no token check/record there.
4. **Frontend**: `useCopilot` handles a `quota_exceeded` event (appends a
   clear "AI token budget exhausted" assistant note). i18n parity.

**Out of scope:** SP4c sandboxed MCP (inv 37), SP4e external-content +
sanitization (inv 19/41). Real-LLM token reporting (the nominal estimate
is the placeholder until a backend reports actuals). A quota-management
UI / admin reset.

## 2. Data Model

`ai_token_usage` (subset of doc §5):
```python
op.create_table(
    "ai_token_usage",
    sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
    sa.Column("tenant_id", sa.BigInteger(),
              sa.ForeignKey("tenants.id"), nullable=False),
    sa.Column("user_id", sa.BigInteger(),
              sa.ForeignKey("users.id"), nullable=True),
    sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=True),
    sa.Column("model_name", sa.String(64), nullable=True),
    sa.Column("tokens_input", sa.Integer(), nullable=False),
    sa.Column("tokens_output", sa.Integer(), nullable=False),
    sa.Column("occurred_at", sa.DateTime(timezone=True),
              server_default=sa.func.now(), nullable=False),
)
op.create_index("idx_ai_usage_tenant_time", "ai_token_usage",
                ["tenant_id", "occurred_at"])
op.add_column("tenants", sa.Column(
    "quota_ai_tokens_month", sa.BigInteger(), nullable=False,
    server_default="1000000"))
```

**Migration-vs-model server_default**: `ai_token_usage.tokens_input/
output` — model uses Python `default`? NO — they're always supplied by
the caller (no default needed); model declares them NOT NULL without a
default, migration likewise without server_default (consistent). `id`
BIGSERIAL = `autoincrement=True` (no uuid). `occurred_at` server_default
`now()` matches the model. For `tenants.quota_ai_tokens_month`: the
column is ADDED to an existing populated table, so it **needs** a
`server_default` in the migration (to backfill existing rows) — but to
avoid `compare_server_default` drift, the **model** column must ALSO
declare the same `server_default` (`server_default="1000000"` +
`default=1_000_000`). (This differs from the SP4a/4b uuid-PK case: there
the column was new in a new table with no rows to backfill, so Python
default sufficed. Here we alter an existing table.)

`AITokenUsage` model in `src/dlw/db/models/ai.py`; registered in
`db/models/__init__.py` + `alembic/env.py`. `Tenant` model gains
`quota_ai_tokens_month` (with matching server_default). `EXPECTED_TABLES`
+= `ai_token_usage`.

## 3. Service (`src/dlw/services/ai_quota.py`)

```python
class AITokenBudgetExceeded(Exception):
    def __init__(self, remaining: int) -> None:
        super().__init__("ai token budget exceeded")
        self.remaining = remaining

async def check_ai_token_budget(session, tenant_id: int) -> int:
    tenant = await session.get(Tenant, tenant_id)
    quota = tenant.quota_ai_tokens_month if tenant else 0
    month_start = datetime.now(UTC).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0)
    used = await session.scalar(
        select(func.coalesce(func.sum(
            AITokenUsage.tokens_input + AITokenUsage.tokens_output), 0))
        .where(AITokenUsage.tenant_id == tenant_id,
               AITokenUsage.occurred_at >= month_start)) or 0
    remaining = int(quota) - int(used)
    if remaining <= 0:
        raise AITokenBudgetExceeded(0)
    return remaining

async def record_ai_token_usage(session, *, tenant_id, user_id,
                                conversation_id, model_name,
                                tokens_input, tokens_output) -> None:
    session.add(AITokenUsage(
        tenant_id=tenant_id, user_id=user_id,
        conversation_id=conversation_id, model_name=model_name,
        tokens_input=tokens_input, tokens_output=tokens_output))
    # caller commits
```

Month boundary = calendar month start (UTC). The sum is over the current
month only (matches "quota_ai_tokens_month").

## 4. `run_chat` integration

```python
async def run_chat(...):
    # SP4d: pre-turn budget gate (inv 18) — block the AI call when over.
    async with session_maker() as q:
        try:
            await check_ai_token_budget(q, principal.tenant_id)
        except AITokenBudgetExceeded as e:
            yield AgentEvent("quota_exceeded",
                             {"metric": "ai_tokens", "remaining": e.remaining})
            return
    # ... existing conversation create + user msg persist + runner loop ...
    # 4. after assistant message persists:
    tin = max(1, len(message) // 4)
    tout = max(0, len("".join(assistant_text)) // 4)
    am.tokens_input = tin
    am.tokens_output = tout
    # (commit am as before)
    try:
        async with session_maker() as u:
            await record_ai_token_usage(
                u, tenant_id=principal.tenant_id, user_id=principal.user_id,
                conversation_id=conv_id, model_name=runner.model_name,
                tokens_input=tin, tokens_output=tout)
            await u.commit()
    except Exception:  # noqa: BLE001 — usage recording is best-effort
        pass
```

The pre-turn check uses its own short session (no lock needed — a soft
budget; a tiny over-spend race across concurrent turns is acceptable for
a spend cap). `run_confirmation` (phase 2) is unchanged (no LLM call).

## 5. Frontend

`useCopilot`: on a `quota_exceeded` event, append to the assistant
message text a localized note (`copilot.quotaExceeded`) and stop. (No
new component; the existing assistant bubble shows the note.) i18n
`copilot.quotaExceeded` in both locales.

## 6. Tests

**Backend** (`tests/ai/test_ai_quota.py`, extend `tests/api/test_ai_chat.py`):
- `check_ai_token_budget`: no usage → returns full quota; usage seeded
  near the limit → returns small remaining; usage ≥ quota → raises
  `AITokenBudgetExceeded`.
- month boundary: a usage row dated last month does NOT count.
- chat over-budget: seed `ai_token_usage` ≥ a tenant's
  `quota_ai_tokens_month` → `POST /ai/chat {message}` → first event is
  `quota_exceeded`, NO conversation row created, NO assistant message.
- chat under-budget: a normal turn → records exactly one
  `ai_token_usage` row for the tenant with tokens_input/output > 0, and
  the assistant `ai_messages` row has matching token counts.
- tenant isolation: tenant-2 usage doesn't count against tenant-1's
  budget.

**Frontend** (`useCopilot.spec.ts` extension): a `quota_exceeded` event
→ assistant message contains the budget note; streaming ends.

## 7. Milestones

- **M1 Backend**: migration + `AITokenUsage` + `Tenant.quota_ai_tokens_month`
  + `ai_quota.py` + `run_chat` integration + stub nominal tokens + tests
  + EXPECTED_TABLES + full backend gate + dev-DB upgrade.
- **M2 Frontend**: `useCopilot` quota_exceeded handling + i18n + test +
  full frontend gate.
- **M3 Smoke + docs**: curl over-budget (seed usage → `quota_exceeded`);
  append SP4d section to `docs/operator/web-ui.md`.

## 8. Risks & Contingencies

- **Migration alters an existing table** (`tenants`): the new NOT NULL
  column needs a `server_default` to backfill existing rows — and the
  MODEL must declare the same `server_default` to avoid
  `compare_server_default=True` drift (env.py enables it). This is the
  one place SP4d diverges from the SP4a/4b "Python-default-only" rule
  (those added columns to NEW tables with no rows). Explicitly tested by
  `test_alembic.py` upgrade-head.
- **Soft budget / race**: the pre-turn check is not locked; concurrent
  turns could each pass the check and slightly overspend. Acceptable for
  a monthly spend cap (not a correctness gate). Documented.
- **Nominal token estimate**: chars/4 is a placeholder until a real
  backend reports actuals; documented. The enforcement + recording
  machinery is the real deliverable.
- **No openapi change / no literal null examples** (SP4a CI lesson).
  `quota_exceeded` is already in the doc's event schema (§4.3).
- **Best-effort usage recording**: a record failure must not lose the
  turn (SP4a CRITICAL-2 pattern) — wrapped in try/except.

## 9. Self-Review

- **Placeholder scan**: the nominal-token estimate is a deliberate,
  documented placeholder (real backends report actuals), not a vague
  TODO.
- **Invariant 18**: token quota separate from download quota (new
  column + table, independent check); exhaustion blocks AI calls only.
- **Migration**: 3rd AI migration; down_revision `a2b3c4d5e6f7`;
  register model in 2 places; EXPECTED_TABLES; the alter-table
  server_default + matching model server_default (drift-safe).
- **Consistency**: reuses the SSE/stub/audit infra; pre-turn gate
  mirrors the existing `quota_exceeded` REST pattern; recording is
  best-effort isolated.
