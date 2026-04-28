# 08 — MVP 切片与里程碑路线图

> 角色：项目经理 / Tech Lead / 团队全员对齐"先做什么、后做什么"。
> 原则：每个 Phase 都是**可独立发布**的产品形态，不是"半成品"。

---

## 0. 总体策略

```
                                     v2.0 GA
  Phase 1                Phase 2              Phase 3           Phase 4
  ─────────────         ───────────────      ─────────────     ─────────────
  4 weeks               3 weeks              3 weeks           3 weeks
  单租户 PoC             分布式生产化           平台化             生产加固
   ↓                     ↓                    ↓                 ↓
  Internal alpha        Internal beta        External beta     GA
```

**关键原则**：

1. **每个 Phase 都有可发布物**，能独立提供价值（不是"半个功能"）
2. **质量门必通**，不达标不进下一 Phase（避免债务雪球）
3. **架构债不留**：fence token / multi-tenancy / mTLS 这些**底盘**在早期 Phase 就铺好，避免后期改造
4. **每 Phase 末做一次架构 review**，根据实测修订后续 Phase

---

## 1. Phase 1 — 单租户 PoC（4 weeks）

### 1.1 目标

跑通端到端：UI 创建任务 → Controller 调度 → 单 Executor 下载 → 校验 → S3。

### 1.2 范围（IN）

- ✅ Controller 单实例 + PostgreSQL
- ✅ Executor 单实例（先不做多 executor）
- ✅ UI 基础（创建任务、查看进度）
- ✅ 单源 HuggingFace（无 ModelScope/Mirror）
- ✅ 单线程下载（先不做 chunk 并发）
- ✅ S3 单 backend
- ✅ 流式 SHA256 校验（不变量 5）
- ✅ 任务状态机（不含 cancelling/paused_external/paused_disk_full）
- ✅ 基础日志（structlog + stdout）

### 1.3 不在范围（OUT，明确推迟）

- ❌ 多租户（hard-code tenant_id=1）
- ❌ Fence token / executor epoch（先用简单 CAS）
- ❌ mTLS（先用简单 token）
- ❌ 多源调度
- ❌ 多 executor 协调
- ❌ Active/standby
- ❌ 配额 / 审计 / 合规
- ❌ Webhook / K8s Operator
- ❌ chunk-level routing
- ❌ Increment download
- ❌ CLI / SDK

### 1.4 入场标准（Phase 0 → Phase 1）

- [x] 设计文档 v2.0 全部完成（00-09）
- [x] 项目骨架 PR：`pyproject.toml`、目录结构、Dockerfile、CI 雏形
- [x] 团队成员就位（后端 ×2、前端 ×1、QA ×1、SRE ×0.5）
- [x] 开发/测试 K8s namespace 就绪

### 1.5 出场标准（Phase 1 → Phase 2）

**功能**：

- [ ] E2E-001 通过：能完成 1 个 GLM-4-9B（18.5GB）从 HF 到 S3 的下载
- [ ] 任务状态机所有合法 transition 单测通过（U-SM-001..003, 012）
- [ ] 任务级最终校验比对所有 sha256（U-VER-001..003）
- [ ] DB schema migration alembic 支持（I-DB-008）

**质量**：

- [ ] 单元测试覆盖率 ≥ 80%（仅 Phase 1 代码）
- [ ] 集成测试 I-CE-001..010 通过
- [ ] 无 high/critical 安全扫描发现
- [ ] OpenAPI 实际 yaml 与代码一致（CI 断言）

**文档**：

- [ ] Phase 1 实施报告（哪些设计在落地中改了）
- [ ] 架构 review：与 v2.0 设计偏差清单
- [ ] 容量基线初测（P-001 部分基线）

### 1.6 Phase 1 任务分解（4 weeks）

```
Week 1: 骨架
  ├─ Day 1-2: 项目结构 + DB schema 落地 + alembic init
  ├─ Day 3-5: Controller FastAPI 框架 + 健康检查 + auth 简化版

Week 2: Controller 核心
  ├─ Day 1-3: 任务 CRUD API + DB layer
  ├─ Day 4-5: 调度循环（无 fence，仅 CAS）

Week 3: Executor + UI
  ├─ Day 1-3: Executor 心跳 + 单线程下载 + S3 multipart
  ├─ Day 4-5: UI 任务列表 + 详情 + WS 进度

Week 4: 校验 + 联调
  ├─ Day 1-2: 流式 SHA256 + 任务级最终校验
  ├─ Day 3-4: E2E-001 联调
  ├─ Day 5: 内部 alpha demo
```

### 1.7 Phase 1 风险

| 风险 | 缓解 |
|------|------|
| HF API 限流影响开发 | 配 wiremock 模拟 HF；只在 Day 5 联调时用真 HF |
| S3 cost 失控 | 用 MinIO 本地 mock；真 S3 测试预算 $50/Phase |
| 团队 Python async 经验不足 | Week 1 安排 2 天 asyncio 培训 |
| 任务状态机改动频繁 | 用 `pytest-yaml` 把 `tasks.yaml` 当 fixture，改 yaml 即更新测试 |

---

## 2. Phase 2 — 分布式生产化（3 weeks）

### 2.1 目标

引入分布式正确性保证，上线"内部 beta"。

### 2.2 范围（IN）

- ✅ Fence token + executor epoch（解决 D1 / D6）
- ✅ 多 executor 协调（含 multi-executor-aware scheduler）
- ✅ 崩溃恢复 三联校验（解决 D2）
- ✅ Multipart upload_id 持久化
- ✅ 节点状态机（含 degraded↔suspect 修复，D3）
- ✅ Cancelling 中间态（解决 D8）
- ✅ Paused_external（解决 D13）
- ✅ Paused_disk_full（解决 D7）
- ✅ mTLS + Executor JWT（解决 SEC-01）
- ✅ HMAC 心跳 + nonce + timestamp（解决 SEC-04）
- ✅ HF Token reverse-proxy（解决 SEC-02）
- ✅ Chunk-level 多线程下载（DirectOffsetDownloader）
- ✅ Active/Standby controller（解决 OPS-04 部分）
- ✅ Recovery routine 完整版

### 2.3 不在范围（OUT）

- ❌ 多源（仅 HF）
- ❌ 多租户（仍 single tenant）
- ❌ 配额 / 审计
- ❌ License / 合规
- ❌ Webhook / K8s Operator
- ❌ 增量 / CLI

### 2.4 入场标准

- Phase 1 出场标准全部满足
- Phase 1 实测 P-005（PG TPS）数据存在；如不达标先优化

### 2.5 出场标准

**功能**：

- [ ] U-SCHED-001..012 全通过（fence token）
- [ ] U-SM-004..011 通过（cancelling、paused_*、状态机）
- [ ] I-CE-001..013 通过
- [ ] E2E-FI-001..006 通过（故障注入）
- [ ] Active/standby 切换 RTO ≤ 10min（CH-Q1 演练）

**安全**：

- [ ] mTLS 证书自动续签
- [ ] HMAC 心跳全链路
- [ ] HF Token 不再下发到 executor（gitleaks 扫执行器代码）

**性能**：

- [ ] P-004：单 executor 下载 ≥ 1GB/s（NIC=10Gbps 链路下）
- [ ] P-001：心跳处理 ≥ 5000 ops/s

### 2.6 任务分解（3 weeks）

```
Week 1: Fence Token + Recovery
  ├─ Day 1-2: assignment_token + executor_epoch DB 列 + alembic
  ├─ Day 3-4: CAS-then-enqueue + complete_subtask fence
  ├─ Day 5: recovery routine 三联校验

Week 2: 多 Executor + 状态机
  ├─ Day 1-2: multi-executor-aware scheduler（不变量 10）
  ├─ Day 3: 节点状态机修复（D3）
  ├─ Day 4: cancelling / paused_* 状态
  ├─ Day 5: chunk-level 多线程下载

Week 3: mTLS + Active/Standby
  ├─ Day 1-2: mTLS CA + executor enrollment + JWT
  ├─ Day 3: HMAC 心跳
  ├─ Day 4: HF reverse-proxy
  ├─ Day 5: Active/standby + chaos 演练
```

### 2.7 风险

| 风险 | 缓解 |
|------|------|
| Active/standby 切换复杂度 | 用 PG advisory_lock 简化方案；先单实例 + manual failover |
| mTLS 证书运维负担 | 自签 CA 24h TTL 自动续签；不引入 cert-manager 在 Phase 2 |
| Fence token 与现有调度逻辑耦合 | 严格走 03 §2 协议；review 必须通过 |

---

## 3. Phase 3 — 平台化（3 weeks）

### 3.1 目标

从"工具"成为"平台"：多租户、多源、CLI、生态集成。

### 3.2 范围（IN）

- ✅ 多租户（OIDC + JWT + RBAC + tenant_id 全表）
- ✅ 配额（bytes / storage / concurrent）
- ✅ 多源（HF + ModelScope + hf-mirror，3 个驱动）
- ✅ NameResolver（identity + 规则映射）
- ✅ LPT file-level routing
- ✅ 启动前实时测速 + 最优组合（06 §1.8）
- ✅ chunk-level 多源 routing（仅 ≥100MB 文件）
- ✅ 增量下载（upgrade_from_revision）
- ✅ 全局去重（refcount）
- ✅ CLI（dlw submit / list / show / cancel / watch）
- ✅ Python SDK（同步 + 异步）

### 3.3 不在范围（OUT）

- ❌ WiseModel / OpenCSG（plugin 机制开放，但不内置）
- ❌ K8s Operator（roadmap）
- ❌ MLflow 集成（roadmap）
- ❌ HF cache 兼容（HF_HOME 透明代理）（roadmap）
- ❌ 离线 / 气隙模式
- ❌ 跨地域复制

### 3.4 入场标准

- Phase 2 出场标准全满足
- 1 个 staging 环境跑了 1 周 phase-2 stable
- 团队增加：前端 ×1（CLI 不算前端）

### 3.5 出场标准

**功能**：

- [ ] U-SRC-* / I-SRC-* 通过
- [ ] E2E-002（多源 auto_balance）通过
- [ ] E2E-MT-* 全通过（多租户隔离）
- [ ] CLI 主要命令通过 acceptance
- [ ] 增量下载 verify 节省 ≥ 90% 流量（同 repo 仅 tokenizer 改动）

**性能**：

- [ ] 多源测速：5 sources × 4 executors 在 8 秒内完成
- [ ] 多源 LPT 实测加速因子 ≥ 2x（vs 单源 HF 中国境内）
- [ ] 跨租户隔离：tenant A 100 任务不影响 tenant B 性能

**用户**：

- [ ] 至少 2 个内部团队（不同 tenant）on-board
- [ ] 反馈：CLI 命令直觉、错误提示清晰、SDK 接入 ≤ 30 分钟

### 3.6 任务分解（3 weeks）

```
Week 1: 多租户底层
  ├─ Day 1-2: tenants/projects/users schema + OIDC 集成
  ├─ Day 3: RBAC (casbin) + 中间件
  ├─ Day 4: 全表加 tenant_id（DB migration）
  ├─ Day 5: 配额表 + 强一致检查

Week 2: 多源
  ├─ Day 1-2: SourceDriver 抽象 + HF + hf-mirror 驱动
  ├─ Day 3: ModelScope 驱动 + NameResolver
  ├─ Day 4: 测速 + LPT routing
  ├─ Day 5: chunk-level routing + 局部重平衡

Week 3: 增量 + CLI/SDK
  ├─ Day 1-2: 增量下载（diff + hardlink/copy）
  ├─ Day 3-4: CLI dlw + Python SDK
  ├─ Day 5: 内部 beta 发布
```

### 3.7 风险

| 风险 | 缓解 |
|------|------|
| 多租户 retrofit 工作量 | Phase 1 时已经有 tenant_id 列（hard-code=1）；Phase 3 仅是逻辑接入 |
| ModelScope API 不稳定 | 测速失败时 graceful fallback；24h 黑名单 |
| LPT 算法在小文件多/大文件少时失衡 | 单测覆盖 corner case（U-SRC-005..006） |

---

## 4. Phase 4 — 生产加固（3 weeks）

### 4.1 目标

生产 GA：安全 / 合规 / 运维 / 可观测性全面达标。

### 4.2 范围（IN）

- ✅ 完整审计日志（链式哈希 + WORM 导出）
- ✅ License 合规 + gated 模型审批
- ✅ Pickle / trust_remote_code 审批工作流
- ✅ Webhook（task.completed / failed）
- ✅ HF cache 兼容（HF_HOME 透明代理）
- ✅ K8s Operator + ModelDownload CRD
- ✅ MLflow Model Registry 集成
- ✅ 完整 SLI/SLO + Pyrra
- ✅ Prometheus 告警 yaml + Alertmanager 路由
- ✅ Grafana dashboard JSON
- ✅ 6 份 Runbook 真实脚本
- ✅ Helm chart 完整版
- ✅ Chaos 演练自动化（chaos-mesh）
- ✅ 性能压测套件全跑通

### 4.3 入场标准

- Phase 3 出场标准全满足
- 法务 / 安全团队预审通过
- 真实生产 cluster 备好

### 4.4 出场标准（GA）

**安全**：

- [ ] OWASP ZAP / 渗透测试报告 0 high
- [ ] SOC2 / ISO 27001 测试用例（C-001..005）通过
- [ ] gitleaks / pip-audit 0 critical

**合规**：

- [ ] 审计链 tamper-evident 验证
- [ ] gated 模型必走审批流
- [ ] License 策略生效

**运维**：

- [ ] Phase 4 chaos 演练 CH-Q1..Q4 全通过
- [ ] 4 个 SLO 7 天 burn rate 数据
- [ ] 6 份 runbook 至少演练 1 次

**性能**：

- [ ] P-006..010 通过（含 1 周 soak）
- [ ] 容量上限确认（1000 executor / single controller）

**文档**：

- [ ] 用户文档（Getting Started, CLI guide, SDK guide, FAQ）
- [ ] 运维文档（部署、运行手册、Runbook 索引）
- [ ] API reference（自动生成自 openapi.yaml）
- [ ] 升级指南（v1.x → v2.0）

### 4.5 任务分解（3 weeks）

```
Week 1: 合规与审计
  ├─ Day 1-2: 审计日志链式哈希 + WORM 导出
  ├─ Day 3: License 策略 + gated 审批工作流
  ├─ Day 4: Pickle/trust_remote_code 审批
  ├─ Day 5: 安全测试套件（OWASP ZAP + 手动）

Week 2: 运维 + 集成
  ├─ Day 1: Webhook + 重试
  ├─ Day 2: HF cache 兼容（HF_HOME mount）
  ├─ Day 3: K8s Operator MVP（仅 ModelDownload CRD）
  ├─ Day 4: MLflow 自动注册
  ├─ Day 5: SLI/SLO + Pyrra + 告警 yaml

Week 3: 上线准备
  ├─ Day 1-2: Grafana dashboard + Runbook 脚本化
  ├─ Day 3: Chaos 自动化 + 演练
  ├─ Day 4: Helm chart + 生产环境部署
  ├─ Day 5: GA 发布
```

### 4.6 风险

| 风险 | 缓解 |
|------|------|
| 合规审批工作流复杂度 | MVP：仅 license deny 强阻断；gated 仅警告（不强阻断） |
| K8s Operator 大坑 | MVP：仅支持 ModelDownload create/delete；不做完整 lifecycle |
| Chaos 演练影响其他系统 | 仅在隔离 cluster；不在 prod 跑破坏性演练 |

---

## 5. 整体依赖图

```
              [设计文档 v2.0]
                    │
          ┌─────────┴─────────┐
          │                   │
   [项目骨架 PR]        [测试基础设施]
          │                   │
          └─────────┬─────────┘
                    │
                    ▼
              ┌──────────┐
              │ Phase 1  │ ── 单租户 PoC
              └────┬─────┘
                   │ 必修：fence token 列已存在但未启用
                   ▼
              ┌──────────┐
              │ Phase 2  │ ── 分布式正确性
              └────┬─────┘
                   │ 必修：tenant_id 列已存在 (=1) 但未启用
                   │ 必修：mTLS / HF proxy 已落地
                   ▼
              ┌──────────┐
              │ Phase 3  │ ── 多租户 + 多源 + CLI
              └────┬─────┘
                   │ 必修：所有底盘就绪
                   ▼
              ┌──────────┐
              │ Phase 4  │ ── 合规 + 运维 + GA
              └──────────┘
```

**禁止跳跃**：每个 Phase 出场标准未满足，不得开 Phase N+1。
**允许并行**：每个 Phase 内部任务可按上述 Day 划分并行。

---

## 6. 角色与人数

| 角色 | Phase 1 | Phase 2 | Phase 3 | Phase 4 |
|------|---------|---------|---------|---------|
| Tech Lead | 1 | 1 | 1 | 1 |
| 后端 | 2 | 3 | 3 | 3 |
| 前端 | 1 | 1 | 2 | 2 |
| SRE | 0.5 | 1 | 1 | 1.5 |
| QA | 1 | 1 | 1 | 1.5 |
| 安全顾问 | 0 | 0.5 | 0.5 | 1 |
| **总计 FTE** | **5.5** | **7.5** | **8.5** | **10** |

13 周（~3 个月）总人月 ≈ **22-25 PM**。

---

## 7. 不在 v2.0 范围（v2.1+ Roadmap）

| 主题 | 计划 | 说明 |
|------|------|------|
| Active-active controller | v2.1 | 当前仅 active/standby |
| 跨地域复制 | v2.1 | DR + auto-replicate |
| SLA 分级（class-of-service）+ 抢占 | v2.1 | 基础优先级在 v2.0 |
| 行为遥测 + 热门模型预热 | v2.1 | 数据驱动平台能力 |
| 离线 / 气隙 export bundle | v2.1 | 金融/政务场景 |
| Sigstore 验签 | v2.2 | 上游 HF 推进 |
| WiseModel / OpenCSG 内置驱动 | v2.2 | 当前仅 plugin 形式 |
| 模型在线量化 | v2.2 | 下载完直接生成 GGUF |
| 多源 chunk-level + BLAKE3 流式哈希 | v2.2 | 需要上游 BLAKE3 |
| 多 controller cluster（按 tenant 分片） | v2.3 | 突破 1000 executor 上限 |

---

## 8. 失败的 Phase 处置

如果某 Phase 出场标准 **未通过 80%**：

1. **延期 ≤ 1 周**：完成关键项，过 review 后通过
2. **延期 1-2 周**：拆 Phase（如 Phase 3 → Phase 3a + 3b），后续 Phase 顺延
3. **延期 ≥ 2 周**：架构 review，可能需要回到设计阶段

每个 Phase 的 retrospective：

- 哪些设计在落地中改了？为什么？
- 哪些测试在 review 时漏了？补到 07 中
- 哪些不变量需要新增 / 调整？
- 团队反馈：流程 / 工具 / 协作

---

## 9. 与其他文档的链接

- 设计：→ [00-INDEX.md](./00-INDEX.md)
- 测试矩阵：→ [07-test-plan.md](./07-test-plan.md)
- 数据迁移：→ [09-migration.md](./09-migration.md)
- 不变量：→ [01-architecture.md](./01-architecture.md) §7
