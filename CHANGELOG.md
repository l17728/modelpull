# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

> ⚠️ **设计阶段说明**：当前版本号是**设计文档版本**（v2.0.X），不是软件 release。
> 软件版本（如 v2.0.0-alpha）将在 Phase 1 启动后开始。

---

## [Unreleased]

### Added (2026-05-26 — v2.1 Sprint 1/3/4)

**SLA 分级**（Sprint 1）：
- `tenants.sla_tier` 列（enum: critical/standard/bulk，默认 standard）
- Scheduler 加权排序：tier_weight × (priority+1)，bulk 30 分钟饿死保护 ×2
- Admission control：busy > 90% 拒 bulk，> 99% 拒 standard
- `PUT /api/v1/tenants/{id}/sla` REST（仅 system_admin，审计）
- `/quota/current` 返回 `sla_tier` 字段
- Settings UI：管理员可下拉修改，普通用户只读 tag

**Physical GC**（Sprint 3）：
- 替换 v2.0 的骨架为真实现：tombstone 清理 + LRU 驱逐
- `POST /api/v1/admin/gc/run`（手动触发）+ `GET /admin/gc/status`
- 全局开关 `DLW_PHYSICAL_GC_ENABLED`（默认 false）
- 每次驱逐写审计日志（action=`physical_gc.evict`）

**跨地域复制（Part 1 — 数据模型 + REST）**（Sprint 4）：
- 新表 `replication_jobs`（partial unique 防同 (object,target) 重复挂起）
- `services/replication.py`：create / list / get / cancel + 6 异常
- `POST/GET/POST cancel /api/v1/replication`（租户隔离 + 审计）

**跨地域复制（Part 2 — 真实字节传输 worker）**（Sprint 5）：
- `services/replication_worker.py`：流式 read → sha 校验 → put target → 记录 StorageObject → 状态机
- 3 次失败指数退避重试（sha 校验失败不重试 — 同一字节再读结果不变）
- 中途取消感知：每 chunk 进度回调查 `status='cancelled'`，立即 abort 不写 target
- skip_existing：通过 `(tenant_id, storage_id, sha256)` UniqueConstraint 自然处理 — phase 1 主动查 + phase 3 抓 IntegrityError 兜底并发竞争
- 限速：按 `DLW_REPLICATION_BANDWIDTH_MBPS`（默认 100 MB/s）token-bucket-equiv
- lifespan worker loop：`DLW_REPLICATION_WORKER_ENABLED`（默认 false）+ FOR UPDATE SKIP LOCKED 选 pending
- `tests/services/test_replication_worker.py`：12 用例，stub client 注入，无真 S3 依赖

### Added (2026-05-26 — AI assistant capabilities + local auth)

**Local username/password auth** (alternative to OIDC for air-gapped deployments):
- New `local_credentials` table + Alembic migration `a1b2c3d4e5f6`
- 5 REST endpoints under `/api/v1/auth/local/*` (login, CRUD users, change/reset password)
- Bootstrap via `DLW_ADMIN_INITIAL_PASSWORD` on first startup
- Frontend Login + Settings updated (Change Password card + User Management for admin)

**AI assistant tool expansion** (now 11 read + 7 write = 18 tools):
- New READ: `dlw_list_storages`, `search_huggingface_models`, `search_modelscope_models`
- New WRITE (each requires user confirmation):
  - `dlw_delete_task`, `dlw_retry_task`, `dlw_upgrade_task`, `dlw_patch_task`
  - `dlw_create_local_user`, `dlw_reset_local_password`, `dlw_set_tenant_quota` (system_admin)
- `dlw_create_task` smoothing: revision defaults to `main`, storage_id auto-picks is_default
- Tool descriptions teach LLM priority: domain tools → web_search → model fallback
- `web_search` default-on (still no-ops without `DLW_AI_WEB_SEARCH_API_KEY`)

**AI assistant UI transparency**:
- Markdown rendering for assistant replies (via `marked`, with HTML sanitization)
- Source attribution badge per reply (`💭 model knowledge` / `🤗 Hugging Face` / etc.)
- Decision chain panel ABOVE reply — chronological thinking + tool calls with inputs and result summaries
- Tools-help panel (default expanded) lists all 18 tools with examples
- Help item moved from Settings to sticky sidebar footer (visible on all pages)

**New REST endpoints**:
- `PATCH /api/v1/tasks/{id}` (priority / source_strategy / source_blacklist)
- `PUT /api/v1/tenants/{id}/quota` (system_admin only)

**Services**:
- `services/task_patch.py` — state-machine check + `SELECT FOR UPDATE`
- `services/tenant_quota.py` — only-on-change audit logging

**Skills bridge for opencode (SP4f)**:
- `ai/opencode_skills.py` — generates a MANIFEST.md catalog of all 18
  tools (with shell-command recipes) and the OpenCodeRunner prepends it
  to every turn. The underlying LLM sees the catalog, picks tools by
  matching the user's question against descriptions, and invokes them
  by shelling out to `dlw` CLI / curl. No MCP server needed.
- `ai/opencode_marker_parser.py` — parses `[[dlw_tool name=... input=...]]`
  / `[[dlw_tool_result name=... ok=...]]` markers that the LLM prints
  around each invocation, converts them into `tool_call` / `tool_result`
  events so the decision-chain UI lights up for opencode the same way
  it does for the stub runner. Marker lines are stripped from the
  user-visible reply.
- Config: `ai_opencode_inject_skills: True` (default).

### Added (2026-05-26 — late: v2.1 Sprint 1 complete)

**v2.1 Sprint 1 ship**: SLA tier (critical / standard / bulk) end-to-end.
Scheduler now weights claim ordering by (tier_weight × priority); quota
admission rejects bulk at ≥90% busy; bulk subtasks pending >30 min get
a starvation bump to standard's weight. system_admin can change per-
tenant tier via the new `PUT /api/v1/tenants/{id}/sla` REST endpoint
(audit-logged before/after) or in the frontend Settings → SLA tier
dropdown; non-admin users see a read-only tag with the current tier.
`/api/v1/quota/current` now also returns `sla_tier`. Gated by env
`DLW_SLA_TIER_ENABLED` (default true) so the new ordering can be
rolled back without redeploy. Tag candidate: **v2.1.0-alpha.1**.

Deliverables:
- Migration c2d3e4f5a6b7 — adds tenants.sla_tier with check constraint
- services/sla_tier.py — tier constants, weights, admission_decision,
  set_tenant_sla_tier with audit
- services/scheduler.py — JOIN to Tenant; SQL CASE for tier weight +
  starvation bump; replaces parent_active EXISTS with JOIN's status
  filter to avoid auto-correlation
- services/quota.py — calls admission_decision after hard quotas
- api/tenants.py — PUT /sla endpoint (system_admin, 403/404/422)
- services/quota_read.py — exposes sla_tier in GET /quota/current
- Frontend: Settings → System card gains SLA tier dropdown (admin) /
  read-only tag (others); QuotaCurrent type updated
- 21 new test cases (16 unit + 3 scheduler integration + 1 admission
  + 1 quota response shape)

NOT in alpha.1 (deferred to Sprint 1.5 or later):
- Prometheus per-tier metrics (requires project-wide instrumentation
  foundation first)

### Docs polish (2026-05-26 — late)

- `README.md` / `README_en.md`: test badge 427 → 1000+; AI Copilot
  marked as v2.0 shipped (was misleadingly tagged v2.1); v2.1 roadmap
  row no longer lists AI Copilot or MCP tools
- `docs/v2.0/12-ai-copilot.md`: top-of-doc banner clarifying the
  MCP-based design described inside was superseded by the SP4f Skills
  bridge during implementation — doc preserved as frozen historical
  design record; invariant 37 stays as historical constraint but no
  longer binds runtime code
- New `docs/operator/runbook-ai-assistant.md` — covers 5-layer
  diagnosis flow and 6 high-frequency failures (network error, stub
  mode confusion, opencode missing, empty decision-chain UI, single-
  tool failure, AI token quota exhausted)
- New `docs/operator/runbook-local-auth.md` — covers admin password
  recovery (with and without a second admin), bootstrap failures
  (incl. the pk_users sequence collision), user mis-deletion,
  OIDC ↔ local auth migration, startup-guard failure modes, audit
  trail queries
- New `docs/operator/sla-slo.md` — internal SLO baseline (controller
  availability, API latency, AI response time, scheduling delay,
  multi-source acceleration ratio, RTO/RPO) + incident severity
  levels + honest "why this is a baseline, not a commitment" section

### Removed (2026-05-26)

- **MCP server from roadmap**: the original v2.1 plan was to expose
  tools through a sandboxed MCP subprocess (invariant 37). Replaced by
  the SP4f Skills bridge which achieves the same end (LLM discovers +
  invokes tools without modelpull-specific code in the LLM client) at
  a fraction of the engineering cost and without MCP-over-stdio's known
  Windows / SelectorEventLoop hazards. The v2.0 design doc
  (`docs/v2.0/12-ai-copilot.md`) and invariant 37 remain as historical
  records of the earlier choice; no new work targets MCP.

### Added (2026-05-07 — design v2.0.13 frozen)
- 3 轮多 agent review（共 15 reviewer 视角）找出 ~150 项问题，全部分 6 PR 修复
- `tools/lint_invariants.py` + 9 个 pytest 单测（CI 集成）
- `docs/operator/onboard-first-executor.md` — mTLS bootstrap 流程（解决 FEAS-03 chicken-and-egg）
- `docs/operator/oidc-setup.md` — Keycloak/Auth0 配置示例 + `dlw admin bootstrap`（解决 FEAS-04 day-zero）
- 7 份新 runbook（RB-13~RB-19）覆盖 AI / Optimizer / Multipart / WSS / Credential / Probe / Audit chain
- 5 个 Grafana dashboard（含 AI Copilot / Optimizer / Enterprise Network）
- 32 条 Prometheus 告警（v2.0 12 条 + v2.1 12 条 + 加 description 全部）
- 12 章新设计文档：`12-ai-copilot.md` / `13-adaptive-download-optimization.md` / `14-enterprise-network-and-rate-limit.md`
- 46 条核心不变量索引（01 §7）含 CI 强制断言

### Changed
- v2.0 GA 路线图：14 周 → **15 周**（Phase 1 5w → 6w，含 dev infra week）；P90 估算 18-19 周
- AI Copilot 从 Phase 4 末灰度 → 砍出独立 v2.1 4-6 周里程碑（避免挤压 K8s Operator/Sigstore/chaos）
- 不变量 2 措辞修订：HF Token 不离开 Controller 仅指 **tenant 级**；executor 本地 OOB 池为例外
- 不变量 19 扩展：任何外部 origin 字段（含 dlw_* 工具 output）必须 sanitize
- AuditChainBroken 告警从 ticket → page（P0 安全事件）
- StorageS3High5xx 告警从绝对 rate → ratio
- README 重写为"design-only"诚实定位（前 round 3 reviewer DX-01 指出绿色 badge 误导）

### Fixed
- **CODE-01** (prod 事故级): `AllExecutorsOffline` (P0) 永不触发 — `dlw_executor_health_score` 没 `status` label，recording rule + alert 用错；改 `dlw_executor_status{status}`
- **CODE-02** (SLO 误报): SLO burn rate 用 `avg_over_time(ratio)` 在零流量时段 NaN 传染；改 sloth 标准
- **CODE-05** (部署 blocker): `vault.example.com:8200` 硬编码；改 helm value `required` + `enabled` 默认 false
- **CODE-06** (安全假象): NetworkPolicy 声称限制 LLM endpoint 但实际 ipBlock 全开；加显著 warning + 4 替代方案
- **CODE-07** (备份"假绿"): `verify-backup.sh` heredoc 残破导致 audit 校验恒返回 0；改 `psql -tAc` + numeric 校验
- **CODE-12**: `promote-standby.sh` 去 `bc` 依赖（macOS oncall 没装）；用 `awk`；加 fence 旧 primary 步骤
- **OPS-V21-01**: 12/31 告警无 runbook_url → 全部加 `description` + `runbook_url`
- **OPS-V21-02**: RB-AI-COST/RB-OPT-STORM/RB-MP-INTEGRITY 链接 404 → 实际写出
- **OPS-V21-03**: RB-03 (DataIntegrityFailure P0) 加 step 0 「30s 冻结同源新任务」
- **OPS-V21-05**: inhibit_rules 扩到全 v2.1 主题（凌晨不再 8 个 PD 同时炸）
- **DIST-V21-01..04**: multipart upload_id 持久化 + part_number bump + WSS push epoch fence + reclaim 主动 push
- **AI-SEC-V21-01**: Unicode NFKC + Cf 移除 + confusables + 语义模式 sanitize（11 unit test）
- **FEAS-01..07**: Phase 1 时长、AI canary 范围、前端 FTE、mTLS bootstrap、OIDC bootstrap 等可行性问题
- 65 项前 2 轮 review 已修；24 项 round 3 critical/high 已修

### Security
- 不变量 30: 本地凭证不出本机；controller 仅知 alias
- 不变量 36: AI sanitize 必经 Unicode NFKC + confusables 检测
- 不变量 37: MCP server 不继承 controller 敏感凭证内存
- 不变量 44: mTLS fingerprint mismatch fail-fast（防 SSL inspection 透明降级）
- 不变量 45: cn zone tenant v2.1 GA 期间禁用 AI Copilot

### Notes
- "本仓库目前 0 issues" 不是项目放弃——是因为还在写设计而非求 bug 报告。设计阶段最有价值的贡献是 design review，欢迎来 [issue 模板](https://github.com/l17728/modelpull/issues/new?template=design_review.yml)

---

## [Design v2.0.0 — 2026-04-28]

### Added
- v1.0 / v1.4 / v1.5 三份历史设计文档合并为统一 v2.0 体系（10 章）
- 5 reviewer 第 1 轮 review 找出 70+ 项问题，全部并入 v2.0 设计

### Changed
- 整体架构：从 v1.x 单文件设计 → v2.0 模块化（按主题切分章节）

---

## [Design v1.5 — superseded]

历史设计版本（详见 `docs/archive/design_document_v1.5_review_e2e.md`）。
不再维护；新工作以 v2.0 为准。
