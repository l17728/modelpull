# 分布式 HuggingFace 模型权重下载系统 — 设计文档 v2.0

> 版本: v2.0 | 日期: 2026-04-28 | 取代 v1.0 / v1.4 / v1.5

---

## 这是什么

一个支持多机并行下载、断点续传、负载均衡的 HuggingFace 模型权重下载系统。
适合下载 TB 级超大模型（如 Kimi-K2 1TB、DeepSeek-V3 689GB）。

v2.0 是对 v1.0 / v1.4 / v1.5 三份历史文档的合并、修正、加固版本。
原文档保留在 `../archive/` 仅供追溯，**实施时以 v2.0 为唯一权威来源**。

---

## 文档结构

按主题切分。每份文档独立成章，互相用链接交叉引用。

### 设计文档（架构与契约）

| 文件 | 主题 | 必读对象 |
|------|------|---------|
| **[01-architecture.md](./01-architecture.md)** | 总体架构、状态机、数据模型 | 所有人 |
| **[02-protocol.md](./02-protocol.md)** | API / 心跳 / WebSocket 协议契约 | 实现 SDK / 集成方 |
| **[03-distributed-correctness.md](./03-distributed-correctness.md)** | Fence token / 恢复语义 / crash-consistency | 后端实现者 |
| **[04-security-and-tenancy.md](./04-security-and-tenancy.md)** | 认证鉴权 / 多租户 / 配额 / 合规 | 安全 / 平台团队 |
| **[05-operations.md](./05-operations.md)** | SLO / Runbook / 备份 / 灰度 / 容量 | SRE / on-call |
| **[06-platform-and-ecosystem.md](./06-platform-and-ecosystem.md)** | 多源 / 增量 / CLI/SDK / 生态集成 / Roadmap | 产品 / 生态 |

### 实施支撑（动工前必读）

| 文件 | 主题 | 必读对象 |
|------|------|---------|
| **[07-test-plan.md](./07-test-plan.md)** | ~450 测试矩阵（unit/integration/E2E/perf/sec/chaos） | QA / 后端 / SRE |
| **[08-mvp-roadmap.md](./08-mvp-roadmap.md)** | 4 Phase 切片 + 入场/出场标准 + 任务分解 | PM / Tech Lead / 全员 |
| **[09-migration.md](./09-migration.md)** | v1.x → v2.0 数据迁移 + alembic + 灰度策略 | DBA / SRE / 后端 |
| **[10-frontend-wireframes.md](./10-frontend-wireframes.md)** | 9 个核心页面 wireframe + Vue3/Pinia 组件设计 | 前端 / UI |
| **[11-cli-and-sdk-spec.md](./11-cli-and-sdk-spec.md)** | `dlw` CLI + Python SDK 完整规范 | CLI/SDK / 文档作者 / 用户 |
| **[../../api/openapi.yaml](../../api/openapi.yaml)** | 完整 OpenAPI 3.1 spec（可生成 client） | 前端 / SDK / QA |

### 部署物料（生产可用）

| 路径 | 内容 |
|------|------|
| `../../deploy/helm/` | Helm chart：Chart.yaml + values.yaml + 7 templates（controller/executor/UI/PDB/NetworkPolicy/CSI/SA/ConfigMap） |
| `../../deploy/prometheus/` | recording-rules.yaml + alerting-rules.yaml（P0/P1/P2 三档分级） |
| `../../deploy/alertmanager/` | routes.yaml（PagerDuty/Slack/Jira 路由 + inhibit_rules） |
| `../../deploy/grafana/` | overview-dashboard.json + slo-dashboard.json（multi-burn-rate） |
| `../../deploy/runbooks/scripts/` | 6 个可执行 runbook 脚本（promote-standby / drain-executor / gc-orphan-parts / rotate-mtls / verify-backup / maintenance） |

---

## 按角色推荐阅读路径

**👨‍💻 架构师 / 评审者**：01 → 03 → 04 → 02 → 05 → 06 → 08（roadmap）

**🔨 后端实现者**：08 → 01 → 02 → 03 → 04 → 05 → 07

**🧪 QA**：07 → 02 → 03（理解状态机）→ 09（升级测试）

**🛡️ 安全审计**：04 → 02 → 01 §3 状态机 → 05 §10 优雅停机

**🚨 SRE / on-call**：05（全部）→ 09 → 03 §3 恢复语义 → 04 §6 DoS

**👤 用户 / 算法工程师**：06 §5 CLI/SDK → 02 §1 协议总览

**🏗️ 平台 / 集成方**：06 → 02 → 04 §1 租户 → openapi.yaml

**📅 PM / Tech Lead**：08（4 Phase 全部）→ 07 §8 测试与 Phase 对应 → 09

---

## v2.0 相对历史版本的变化（速读）

### 解决的关键问题（按严重度）

🔴 **Critical（修复完）**

- **架构一致性**：任务状态机三处定义统一；心跳/任务模型字段漂移收敛；v1.0 旧章节标 superseded
- **分布式正确性**：引入 fence token + executor epoch 防双发；崩溃恢复加三联校验；multipart upload_id 持久化
- **安全**：Executor 注册 mTLS + JWT；HF Token 改 reverse-proxy 不下发；REST/WS 全 OIDC + JWT；强制 revision=&lt;sha&gt; + 全文件 SHA256 + pickle 拦截
- **运维**：4 个核心 SLI/SLO；6 份 runbook；PG WAL backup + RPO 15min；Controller active/standby 提前到 v1
- **平台盲区**：Tenant/Project/User 三级身份；租户级配额与计量；License/合规治理

🟡 **High（修复完）**

- 取消 Executor `_task_poll_loop` 双路径；UI 不直连 HF / 不做调度决策
- 节点状态机消除 degraded↔suspect 死循环
- CDN URL 失效后 commit pin 防错拼
- OpenTelemetry traces 真正埋点；告警 hysteresis + inhibition
- 多源镜像（HF / hf-mirror / ModelScope）；增量 diff 下载；CLI/SDK；MLflow/K8s 集成

🟢 **Medium → Roadmap**：DR / SLA 分级 / 行为遥测预热 / active-active —— 见 06 §9。

### 不再有效的内容

- v1.0 §5（控制器）、§6（执行器）、§8（调度）、§13（API）—— 整段被 v2.0 取代
- v1.4 §4.1 任务状态机图、§8.6 状态机图 —— 内部矛盾，统一以 01 §3 为准
- v1.4 §6.4 / §6.8 / §12.5 文件进度矩阵 UI 重复绘制 —— 统一以 06 §7 为准

---

## 阅读约定

- **🔒 不变量**：标记后续实现绝对不能违反的属性，CI 应有断言
- **⚠️ 已知风险**：当前设计已识别但未解决的问题
- **📝 决策**：选型决策与放弃的备选项
- **➡️ 跨链接**：跳转到其他章节
- **代码块**：
  - `python` 块为示例伪代码，非可直接运行实现
  - `sql` 块为权威 schema，应作为 migration 蓝本
  - `yaml` 块为权威配置 schema

---

## 文档维护

- 一处修改、多处引用：所有跨文档引用用相对链接，避免复制粘贴
- 数据模型与 API 字段：仅在 `01-architecture.md` §4 和 `02-protocol.md` §2 OpenAPI schema 中定义。其他文档只引用，不重复
- 状态机：仅在 `01-architecture.md` §3 中定义。其他文档只引用，不重画
- 修改日志：在本文件末尾追加（不要 inline 修改章节标记 v2.1 等版本号）

### 修改日志

| 日期 | 版本 | 修改 |
|------|------|------|
| 2026-04-28 | v2.0 | 初版：合并 v1.0 / v1.4 / v1.5，修复五位 reviewer 提出的 70+ 条问题 |
