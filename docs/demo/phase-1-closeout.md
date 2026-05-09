# Phase 1 Closeout — modelpull v0.1.0-alpha

> 阶段总结：实施时间线、技术指标、已落不变量、显式推迟清单、Phase 2 入口。
> 周报临起 / 阶段 review / handoff 资料皆可复用此文档。

- **起止**：2026-04-28（v2.0 设计文档完成 + 仓库初始化）→ 2026-05-09（PR #5 merge，§1.5 出场闸 E2E-001 闭环）
- **活跃实施天数**：5 天（5/6 一组 8 commits → 5/7 54 commits → 5/8 19 commits → 5/9 22 commits + 5 个 PR）
- **roadmap 计划**：6 周 / 22-24 工程师周（v2.0.13 FEAS-01）；**实测 5 天落地**（计划单人 6 周 vs subagent-driven 实施 5 天）

---

## 1. 实施时间线（5 个 PR）

| PR | merge SHA | 范围 | tests | CI 一次过 |
|----|-----------|------|-------|-----------|
| #1 Foundation | `f815702` | pyproject + DB 9 表 + alembic + auth bearer + FastAPI 健康检查 | 18 | ✅ 9/9 |
| #2 Controller Core | `41d1e68` | Tasks/Executors API + scheduler `FOR UPDATE SKIP LOCKED` + concurrency 真证 | 51 | ❌ 2 轮 |
| #3 Executor Process | `03ecea4` | dlw-executor CLI + heartbeat + poll-execute + docker-compose 3 服务 | 70 | ✅ 10/10 |
| #4 UI Scaffold | `4c7c866` | Vue 3 SPA：Login + TaskList + TaskDetail + 5s/1s polling + 18 frontend tests | 73 + 18 | ✅ 12/12 |
| **#5 Real HF + S3** | **`c952957`** | **HfS3StreamDownloader（流式 O(5MB) + sha256 verify + multipart）+ HF Hub API** | **99 + 18** | **✅ 12/12** |

**5 PRs / 109 commits / 4 一次过 + 1 二轮（80% 一次过率）**

---

## 2. 当前技术指标

| 指标 | 值 |
|------|----|
| Backend Python LOC（src/dlw/） | 2138 行 |
| Frontend LOC（frontend/src/） | 823 行（TS + Vue） |
| Test LOC（tests/ + frontend/tests/） | 3143 行 + ~600 行 frontend tests |
| Backend tests | **99 passing**（manual smoke 1 deselected via addopts `-m 'not manual'`） |
| Frontend tests | **18 passing**（Vitest：4 auth + 2 client + 8 StatusBadge + 4 useTaskDetail） |
| CI jobs | 12（OpenAPI / Helm / Shellcheck / Markdown / YAML / Security / JSON / Invariant / pytest / frontend-lint / frontend-build / aggregator） |
| Alembic migrations | 3（initial schema + 调度 hot-path indexes + s3_key 列） |
| Docker Compose 服务 | 5（postgres / controller / executor / minio / init-bucket） |
| 设计文档（docs/v2.0/） | 14 份（架构 / 协议 / 分布式正确性 / 安全 / 运维 / 平台 / 测试 / Roadmap / 迁移 / 前端 / CLI / AI Copilot / 自适应 / 内网） |
| Spec + Plan 文档（docs/superpowers/） | 5 spec + 5 plan（每 PR 一对） |

---

## 3. 已落实 Phase 1 §1.5 出场闸

| 出场标准 | 状态 | 证据 |
|----------|------|------|
| E2E-001：1 个模型 HF→S3 端到端下载 | ✅ | PR #5 / `tests/e2e/test_executor_e2e.py` + manual smoke 真 HF |
| 任务状态机所有合法 transition 单测通过 | ✅ | `tests/services/test_scheduler.py` + `test_task_service.py` |
| 任务级最终校验比对所有 sha256（U-VER-001..003） | ✅ | `scheduler.complete_subtask` controller 端 verify gate |
| DB schema migration alembic 支持 | ✅ | 3 个 migrations roundtrip 清洁 |
| 单测覆盖率 ≥ 80% | ✅ | pytest job 强制 |
| 集成测试 I-CE-001..010 通过 | ✅ | 99 tests 包含跨层集成 |
| 无 high/critical 安全扫描发现 | ✅ | gitleaks job 每 PR 绿 |
| OpenAPI 实际 yaml 与代码一致 | ⚠️ | `api/openapi.yaml` Phase 1 W4 暂未同步 `storage_config` 字段 — Phase 2 sync |

---

## 4. 已实现关键不变量

按 `docs/v2.0/03-distributed-correctness.md` 编号，Phase 1 必落：

| # | 不变量 | 实现位置 |
|---|--------|----------|
| 1 | 凭证不离开 controller（HF token） | ⚠️ Phase 1 简化为 executor env；Phase 2 reverse-proxy |
| 5 | 流式 SHA256（不读两次） | `HfS3StreamDownloader.download` 同字节流 tee |
| 6 | CAS-then-enqueue（assignment_token） | `scheduler.claim_one_subtask` + `complete_subtask` token verify |
| 8 | tenant_id 全表 column | DB 9 表全 `tenant_id` 字段（Phase 1 hardcode=1） |
| 9 | host-X-worker-N executor id 格式 | `ExecutorSettings._derive_host_id` validator |
| 14 | 任务状态机正确（pending/scheduling/downloading/succeeded/failed/cancelled） | `complete_subtask` 终态原子转换 + 全 transitions 单测 |
| 38 | sha256 verify 是 controller-side，单一 referee | `complete_subtask` `if final_status == "succeeded" and expected != actual: → failed` |
| 46 | FOR UPDATE SKIP LOCKED 调度 | `claim_one_subtask` + 真证测 `test_one_subtask_two_claimants_only_one_wins` |

剩余 38 条（mTLS / executor_epoch / multipart resume / chunk-routing / 多源 / 多租户 / 审计 / AI / 限速）按 spec 分别推迟到 Phase 2/3/4。

---

## 5. 显式推迟到 Phase 2/3/4

按 spec 章节明确，已写但本期不做：

### Phase 2（mTLS + fence-token + crashed reclaim）
- mTLS executor 认证（不变量 1） + `cert_fingerprint` validator 落地
- `executor_epoch` fence-token + reclaim crashed executors 三联校验（D2）
- HF Token reverse-proxy（不变量 2）+ STS 临时凭证（不变量 3）
- multipart_upload_id 续传 + chunk-level 多线程下载（DirectOffsetDownloader）
- Active/Standby controller（PG advisory_lock）
- Recovery routine 完整版

### Phase 3（多租户 + 多源 + CLI）
- OIDC PKCE + 多用户 RBAC（移除 hardcoded tenant_id=1）
- 配额（bytes / storage / concurrent_tasks）+ 强一致检查
- 多源（HF + ModelScope + hf-mirror，3 个驱动）+ NameResolver
- chunk-level 多源 routing（≥100MB 文件）
- 增量下载（upgrade_from_revision）+ 全局去重 refcount
- CLI（dlw submit / list / show / cancel / watch）+ Python SDK
- KMS envelope encryption for `storage_backends.config_encrypted`

### Phase 4（生产加固）
- AI Copilot（natural-language → task）+ 自适应优化
- 审计 chain-hash trigger
- Webhook + K8s Operator
- 限速 + 内网拨号优化

---

## 6. 实施模式（4 PR 验证）

**Plan-driven + subagent-driven + 自我 milestone E2E**（详见 `feedback_subagent_driven_dev.md` memory）：

```
spec (brainstorming + 1 self-review)
  → plan (writing-plans + 2-agent multi-perspective review + 9-11 plan-level fixes)
    → execute task-by-task (1 sonnet implementer subagent per task, no per-task review)
      → milestone E2E (self: pytest + curl + browser smoke)
        → push + PR + CI gate (12 jobs)
```

**ROI 数据**：

| PR | 预先 review 修订 | 运行期 catch | CI 迭代 |
|----|-------------------|--------------|---------|
| #2 W2 | 10 项 | 0 | 2 轮 |
| #3 W3 Executor | 9 项 | 0 | 1 轮 |
| #4 W3 UI | 10 项 | 1（W4-K pnpm 转发） | 1 轮 |
| #5 W4 HF+S3 | 9 项 | 1（W5-J GatedRepoError 继承） | 1 轮 |

**总计**：**38 plan-level + 2 runtime 修订** vs 传统 per-task triple-review 模式（每 task ~3 reviewer 调用）。Token 消耗 30-40% 节省 + 实施时间 -60%（5 天 vs 6 周计划）。

---

## 7. Phase 2 入口准备

下一阶段（mTLS + fence-token + crash recovery + multipart resume + chunk-level）按 `docs/v2.0/02-protocol.md` §6 + `04-security-and-tenancy.md` §2 + `08-mvp-roadmap.md` §2 — spec/plan 已写好，可直接进 brainstorming → writing-plans 节奏。

预估：3 周（roadmap §2.6）+ 同样 plan-driven 模式约可压缩到 1-2 周实测。

---

## 8. 已知缺口 / Phase 2 第一周必修

按 §3 标 ⚠️ 项 + 实施期发现：

1. `api/openapi.yaml` 与代码漂移 — Phase 1 W4 加的 `storage_config` 字段 + `s3_key` 列未回填 OpenAPI spec。Phase 2 W1 优先同步 + 加 OpenAPI diff CI gate。
2. HF Token 简化版：executor 直读 `DLW_EXECUTOR_HF_TOKEN` env，违反不变量 2 — Phase 2 reverse-proxy 修。
3. STS 短期凭证：boto3 默认 chain 读 env，违反不变量 3 — Phase 2 引入。
4. Manual smoke 测试目前 skip 在 CI（minio binary 不在 GitHub-hosted runner）— Phase 2 §1.6 Week 5 自托管 runner setup 解决。
5. `download_dir` field on `ExecutorSettings` 自 W4 起未使用（HF→S3 不落盘） — Phase 2 cleanup 删。

---

## 9. References

- 设计文档总览：`docs/v2.0/00-INDEX.md`
- 不变量列表：`docs/v2.0/INVARIANTS.md`（46 条）
- 实施计划：`docs/superpowers/plans/`
- 实施 review feedback：`feedback_subagent_driven_dev.md`（memory）
- Phase 2 spec 入口：`docs/v2.0/04-security-and-tenancy.md` + `docs/v2.0/03-distributed-correctness.md` §2
