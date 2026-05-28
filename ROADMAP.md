# Roadmap

> 这是 [`docs/v2.0/08-mvp-roadmap.md`](./docs/v2.0/08-mvp-roadmap.md) 的 1 页摘要。
> 详细 phase 入场/出场标准、任务分解、风险见原文档。

---

## 📊 当前状态：✅ **v2.0 + v2.1 已实现并合并 · v2.1.0-rc.1**

```
┌────────────────────────────────────────────────────────────────┐
│ ✅ 设计 (已完成):     28000+ 行 / 14 章 / 46 不变量              │
│ ✅ Phase 1-3 (已 ship): 基座 + 分布式正确性 + 多租户/多源/CLI   │
│                       (PR #1–#18，CI 全程一次过)                 │
│ ✅ AI Copilot (已 ship): opencode 无头 + 21 工具 Skills bridge   │
│ ✅ v2.1 (已 ship):    SLA 分级 / Physical GC / 跨地域复制 /      │
│                       自适应运筹优化 / 企业内网 (15 sprint)      │
│                       → v2.1.0-rc.1 (2026-05-27)                 │
│                                                                  │
│ 🚧 v2.1.0 GA (待运维): 7-day staging 压测 + chaos drill +        │
│                       sla-slo 容量基线填回，然后 tag GA          │
└────────────────────────────────────────────────────────────────┘
```

---

## 短期 (v2.0 GA)

### Phase 1 — 单租户 PoC（6 周）

**目标**：跑通 UI → Controller → 单 Executor → 下载 → S3 端到端。

入场：
- [x] 设计文档 v2.0 全部完成
- [x] 仓库 + CI 就绪（9 jobs 全绿）
- [x] OpenAPI spec 完整
- [x] Helm + Prometheus + Grafana + Runbook 雏形
- [ ] 项目代码骨架 PR（pyproject.toml / Vue 3 / Dockerfile）
- [ ] 团队成员就位（后端 ×2、前端 ×1、QA ×1、SRE ×0.5）

出场（满足 100% 才进 Phase 2）：
- [ ] E2E-001 通过：完成 GLM-4-9B（18.5 GB）从 HF 到 S3 端到端
- [ ] 状态机所有合法 transition 单测通过
- [ ] 任务级最终校验比对所有 sha256
- [ ] 单元测试 ≥ 80% 行覆盖（仅 Phase 1 代码）

### Phase 2 — 分布式生产化（3 周）
含：fence token、recovery 三联校验、多 executor、cancelling、mTLS、HF reverse-proxy、Active/Standby

### Phase 3 — 平台化（3 周）
含：多租户、配额、多源（HF + ModelScope + hf-mirror）、增量下载、CLI、Python SDK

### Phase 4 — 生产加固（3 周）
含：审计链、License/gated、Webhook、HF cache 兼容、K8s Operator、SLO/Pyrra、6 runbook、Helm chart 完整版、Chaos 演练、GA

---

## 中期 (v2.1)

独立 4-6 周里程碑。**修订（v2.0.13）：AI Copilot 不在 v2.0 GA**，整体推到 v2.1。

| 主题 | 详情 |
|------|------|
| 🤖 **AI Copilot** first-class | 12-ai-copilot.md 全部能力（写工具 + web_fetch + 多 backend） |
| 📐 **自适应下载运筹优化** | 13-adaptive-download-optimization.md（含 sub-chunking + S3 multipart 多 executor） |
| 🏢 **企业内网部署** | 14-enterprise-network-and-rate-limit.md（NTLM/Kerberos/SSL inspection/凭证池/Live Console） |
| 跨地域复制 | DR + auto-replicate |
| SLA 分级 + 抢占 | class-of-service |
| 行为遥测 + 热门预热 | 数据驱动平台能力 |
| 离线 / 气隙 export bundle | 金融/政务场景完整闭环（v2.0 已含 import 最小路径） |

---

## 长期 (v2.2+)

| 版本 | 主题 |
|------|------|
| v2.2 | Active-active controller / Sigstore 验签 / 模型在线量化 / WiseModel/OpenCSG 内置 / BLAKE3 流式哈希 |
| v2.3 | 多 controller cluster（按 tenant 分片，突破 1000 executor 上限） |

---

## v2.0 GA 阻断项（4 项必修）

| ID | 项 | 状态 |
|----|----|------|
| P-011 | 1000-executor 容量测试通过 | ⏳ Phase 4 末必跑（不通过则 推迟 GA 或下调 SLO，需 tech-lead + ops-lead 联签） |
| ENT-QA-01 | NTLM/Kerberos SPIKE | ⏳ Phase 2.5 计划已写 |
| ENT-QA-02 | SSL inspection 兼容验证 | ⏳ Phase 4 |
| AI-SEC-V21-11 | cn 数据出境合规决策 | ✅ tenants.ai_data_residency_zone 字段已加；v2.1 GA 期间 cn zone 禁用 AI Copilot |

---

## 不在 modelpull 范围

| 主题 | 推荐替代方案 |
|------|------------|
| 单机小模型下载 | `huggingface_hub.snapshot_download` |
| 模型推理 / serving | vLLM, TGI, TensorRT-LLM |
| 模型训练 | DeepSpeed, Megatron-LM |
| 模型版本管理 / 实验追踪 | MLflow, W&B（modelpull 仅集成 webhook） |

---

## 进度追踪

实际进度 → commit 历史（`feat(v2.1 sprint N)` 系列）+ [`docs/v2.1-sprint-plan.md`](./docs/v2.1-sprint-plan.md) live 进度表。

每个 PR 的 commit 信息含 fix 编号（如 `[review-pr5]` 或 `[review-pr6]`），用于追溯到 reviewer 报告。

---

## 与其他文档

- 完整 4 phase 任务分解 → [`docs/v2.0/08-mvp-roadmap.md`](./docs/v2.0/08-mvp-roadmap.md)
- v2.1+ 能力 roadmap → [`docs/v2.0/06-platform-and-ecosystem.md`](./docs/v2.0/06-platform-and-ecosystem.md) §9
- 修改日志（每个 v2.0.X 是什么变了） → [`docs/v2.0/00-INDEX.md`](./docs/v2.0/00-INDEX.md) "修改日志"
