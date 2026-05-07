# 分布式 HuggingFace 模型权重下载系统 — 设计文档 v2.0

> 版本: v2.0 | 日期: 2026-04-28 | 取代 v1.0 / v1.4 / v1.5
>
> 仓库: **https://github.com/l17728/modelpull** · [Issues](https://github.com/l17728/modelpull/issues) · [Discussions](https://github.com/l17728/modelpull/discussions) · [CI 状态](https://github.com/l17728/modelpull/actions)

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
| **[12-ai-copilot.md](./12-ai-copilot.md)** | AI Copilot 嵌入式聊天 + 无头 Agent + MCP 工具（v2.1 first-class） | AI/产品 / 后端 / 前端 / 安全 |
| **[13-adaptive-download-optimization.md](./13-adaptive-download-optimization.md)** | 在线运筹优化：成本建模 / 子分片 / S3 multipart 多 executor 协作（v2.1） | 调度 / 后端 / 算法 |
| **[14-enterprise-network-and-rate-limit.md](./14-enterprise-network-and-rate-limit.md)** | 反向 WSS 通道 / 限速维度探测 / 凭证池 / 别名 / Live Console（v2.1） | 内网部署 / 运维 / 前端 |
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

**👨‍💻 架构师 / 评审者**：01 → 03 → 04 → 02 → 05 → 06 → 12 → 13 → 14 → 08（roadmap）

**🔨 后端实现者**：08 → 01 → 02 → 03 → 04 → 13 → 14 → 05 → 07

**🧪 QA**：07 → 02 → 03（理解状态机）→ 09（升级测试）→ 12-14（v2.1 测试）

**🛡️ 安全审计**：04 → 02 → 01 §3 状态机 → 05 §10 优雅停机 → **12 §6 AI 安全** → **14 §3 凭证池**

**🚨 SRE / on-call**：05（全部）→ 09 → 03 §3 恢复语义 → 04 §6 DoS → **14 §1 反向通道** → **14 §5 Live Console**

**👤 用户 / 算法工程师**：06 §5 CLI/SDK → 02 §1 协议总览 → **12 §8 AI Copilot UX**

**🏗️ 平台 / 集成方**：06 → 02 → 04 §1 租户 → openapi.yaml

**📅 PM / Tech Lead**：08（4 Phase 全部）→ 07 §8 测试与 Phase 对应 → 09

**🤖 AI / 应用**：12 → 02 §5 (SSE) → 04 §6 (安全)

**📐 调度 / 算法**：13 → 06 §1.6 §1.8（前期反应式版） → 03 §2（fence）

**🏢 内网 / 运维**：14 → 04 §3（凭证差异） → 05 §1.2（日志） → 13 §4.1（限速联动）

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
| 2026-04-28 | v2.0.1 | 加入 07-test-plan / 08-mvp-roadmap / 09-migration / 10-frontend-wireframes / 11-cli-and-sdk-spec |
| 2026-04-28 | v2.0.2 | 加入 deploy/（Helm + Prometheus + Grafana + Runbook 脚本）|
| 2026-04-28 | v2.0.3 | 上线 GitHub 仓库 l17728/modelpull；加入 CI workflow（8 jobs 全绿）+ Issue/PR 模板 + CONTRIBUTING.md |
| 2026-05-06 | v2.0.4 | 加入 12-ai-copilot.md（嵌入式 AI 聊天 + Claude/OpenCode headless + MCP 工具）；07 §9 新增 AI 测试矩阵（unit 80 / mock-LLM 50 / LLM-as-judge 30 / 安全注入 20 / 性能 6）；不变量 15-19 |
| 2026-05-06 | v2.0.5 | 加入 13-adaptive-download-optimization.md（在线运筹优化 + 子分片 + S3 multipart 多 executor 协作）；不变量 20-26；06 §1.8 标记为反应式 baseline，被 13 替代 |
| 2026-05-06 | v2.0.6 | 13 §4.3 触发时机展开：三级触发 + 自适应周期 + 瓶颈聚焦 + 信息门控 + 触发预算；不变量 27 |
| 2026-05-06 | v2.0.7 | 加入 14-enterprise-network-and-rate-limit.md（反向 WSS 通道 + 限速维度探测 + 本地凭证池 + 别名 + Live Console + S3 直连切片）；不变量 28-31；07 §11 新增 ~50 测试 |
| 2026-05-06 | v2.0.8 | **5 reviewer 综合 review** 后修复 12 项 Critical（PR 1）：01 §4.7 v2.1 数据模型同步（5 表+多列+全部加 tenant_id）；不变量 2/19 措辞修订 + 新增 32-38（multipart 持久化/recovery 屏障/CompleteMultipartUpload 校验/WSS push epoch fence/Unicode sanitize/MCP 沙箱/anytime LP）；01 §3.2.1 引入 split parent 状态机；OpenAPI 补 5 v2.1 端点；OR §1.2 plan 离散化 + §1.4 switch_loss_factor 分场景 + §5.4 三向对账 + §5.5 part_number bump；12 §6.1 加 Unicode NFKC + Cf 移除 + confusables 防御层；audit_search 从 MCP 暴露面下架；07 §9 新增 11 个 Unicode 注入测试 |
| 2026-05-06 | v2.0.9 | **PR 2** 修 22 项强烈建议：AI 安全（cross-conv 隔离/modified_input 重检查/LLM 输出审计/per-user 配额/web_fetch T1-T2 分层/multi-turn 测试），OR（lex 目标函数/paired t-test/CUSUM hysteresis），分布式（optimization_generation fence/probe leader election/credentials drain-purge 2-phase），一致性（INDEX 阅读路径/不变量 22 措辞），roadmap（Phase 1 4w→5w 总线 13w→14w），集中化凭证管理（Vault/ExternalSecrets），部署物料（Alertmanager v2.1 12 告警/3 份 Grafana dashboard/NetworkPolicy 含 WSS+AI），不变量 39-43（含 conversation 隔离/modified_input 重校验/T2 边界/lock 覆盖 apply/probe leader） |
| 2026-05-06 | v2.0.10 | **PR 3** 修 25 项 Medium：AI（system prompt leaking 防御/read-write 限额分离 30/3），分布式（WSS 连接层降级子状态/dedup key 跨 executor/actual_outcome race），OR 严谨性（LPT 命名澄清为 greedy heuristic/α=1.0/SAVINGS_NOISE relative/cooldown 公式追溯/跨任务 round-robin 公平兜底/double-axis probe），测试矩阵扩（AI 工具层 13×7=90/Optimizer unit 18→35/E2E 配额边界/i18n 10/升级 7/月度 chaos 5/容量 P-011），跨文档（14 §10 链接修正/Source 生命周期/不变量 11/24/30 验证手段细化），工程（Phase 4 audit 提早 / Sigstore 推迟/前端 Phase 3 起 ≥2 FTE/mutation 关键路径 85%/凭证日志月分区/Console buffer Loki 预热/burst tolerance/bundle import v2.0 最小路径/probe budget 耗尽 fallback） |
| 2026-05-06 | v2.0.11 | **PR 4 治理** 修 6 项：14 §1.4 NTLM/Kerberos 详细 + §1.5 SSL inspection 应对（含 mTLS fingerprint fail-fast 不变量 44）；12 §11.3 cn 数据出境治理决策（v2.1 GA 期间 cn zone 禁用 AI Copilot；不变量 45）；07 §4.3 P-011 1000-executor 容量测试详细计划（GA 阻断项；不变量 46）；新增 `tools/lint_invariants.py`（Python，180 行）+ CI workflow 加 invariant_lint job；`.github/PULL_REQUEST_TEMPLATE.md` 加 DB schema 变更强制 checklist；07 §11.7 加 7 个 NTLM/Kerberos/SSL 测试用例 |
| 2026-05-07 | v2.0.12 | **第3轮 review · PR 5（紧急）** 修 7 项真实代码 bug（防 prod 事故）：CODE-01 P0 告警 `AllExecutorsOffline` 永不触发 → 修 recording rule 用 `dlw_executor_status{status}`；CODE-02 SLO burn rate 公式 → 改 sloth 标准（独立 short/long error_ratio）；CODE-05 `vault.example.com` 硬编码 → helm value `required`；CODE-06 NetworkPolicy FQDN 限制添加显著 ⚠️ warning；CODE-07 verify-backup heredoc 残破（audit 校验恒为 0 假绿）→ 改 psql -tAc + isready 前置；OPS-V21-02 RB-AI-COST/RB-OPT-STORM/RB-MP-INTEGRITY 引用 404 → 实际写出 RB-13~RB-19 共 7 份新 runbook；OPS-V21-01 12 个无 description/runbook_url 告警全部补齐；StorageS3High5xx 改 ratio；AuditChainBroken 升 P0 |
| 2026-05-07 | v2.0.13 | **PR 6（可行性 + 运维稳健）** 修 12 项：FEAS-01 Phase 1 5w→6w（被遗忘的工程任务专列 Week 5）；总线 14w→15w；P90 18-19w 写入 §10；FEAS-07 砍 Phase 4 末 AI canary（独立 v2.1 4-6w 里程碑）；FEAS-05 前端 ≥2 FTE 入场标准对齐；OPS-V21-03 RB-03 加 step 0「冻结同源」+ multipart abort + affected blobs 报表；OPS-V21-05 inhibit_rules 扩到全 v2.1 主题；OPS-V21-09 RB-01 加 status page + UI banner 自动化 + 前置 checklist；CODE-04 preStop sleep magic → helm value 化；CODE-12 promote-standby.sh 去 bc 用 awk + fence 旧 primary（label + cordon）；CODE-03 lint_invariants.py 空目录/缺文件 friendly exit；CODE-08 加 9 个 unit test（pytest）+ CI 集成；新增 `docs/operator/onboard-first-executor.md`（FEAS-03，mTLS bootstrap 流程）+ `docs/operator/oidc-setup.md`（FEAS-04，Keycloak/Auth0 配置示例 + dlw admin bootstrap） |
