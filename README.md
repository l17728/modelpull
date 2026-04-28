# modelpull

> **分布式 HuggingFace 模型权重下载系统** · 多机并行 · 多源加速 · 断点续传 · 完整性校验

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)
![Status](https://img.shields.io/badge/status-design--complete-green)
![Version](https://img.shields.io/badge/version-v2.0--design-orange)

`modelpull` 是一套面向大型语言模型权重下载的分布式系统。专为 TB 级模型（如 DeepSeek-V3 689 GB、Kimi-K2 1 TB）设计，单机下载耗时极长，本系统通过多机协调 + 多源加速将下载时间压缩到接近"出口带宽总和"。

⚠️ **当前阶段：设计文档完成 · 代码实现尚未开始**。本仓库目前包含完整的设计、架构、部署、迁移、测试方案，可作为类似系统的设计参考，或本项目实施的蓝本。

---

## 为什么做这个

```
DeepSeek-V3 (FP8)            689 GB / 163 文件
Kimi-K2-Instruct (FP8)     1,030 GB / 61 文件
Qwen3-72B-Instruct (BF16)    144 GB / 30 文件
```

单机从 HuggingFace 下载这些模型：
- 国外环境：百兆带宽下需要 8-24 小时
- 国内环境：HF 直连不可用，必须走镜像
- 单机故障 / 中断：从头再来

**多机并行** 把整体下载时间压缩到 **`max(每台机/每源限速)`**；**多源加速** 进一步把时间压到 **`总流量 / 各源带宽之和`**。

---

## 核心特性

### 🚀 多源调度（v2.0 头号特性）
内置 6 个源驱动：HuggingFace · hf-mirror.com · ModelScope（魔搭）· WiseModel · OpenCSG · 自托管 S3 mirror。

**一键多源加速**：
1. 任务启动时**实时测速**所有候选源（5-15 秒）
2. 用 LPT 启发式做**最优组合选择**（不一定全用，避免慢源拖累）
3. 文件级路由 + 大文件 chunk 级并行
4. 局部重平衡：源退化自动切换

### 🔒 分布式正确性
- **Fence token + executor epoch**：防止双发 / 陈旧执行器写入
- **三联校验崩溃恢复**：远端存在性 + ChecksumSHA256 + size，绝不假设"DB 标记 verified = 真的 verified"
- **Multipart upload_id 持久化**：崩溃后能 abort 孤儿 multipart
- **HF 是 SHA256 真值来源**：跨源下载完成后用 HF 的 sha 校验

### 🛡️ 安全 / 多租户 / 合规
- mTLS + Executor JWT + 心跳 HMAC
- HF Token reverse-proxy（永不下发到 executor）
- S3 STS 临时凭证
- 三级身份模型（Tenant / Project / User）+ OIDC + RBAC（casbin）
- License 策略 / gated 模型审批 / pickle 拦截
- 审计日志链式哈希（tamper-evident）+ WORM 导出

### 📊 生产可运维
- 4 个核心 SLI/SLO（API 可用性 99.9% / 任务完成率 99% / 吞吐 / E2E 时延）
- 20+ Prometheus 告警（P0/P1/P2 三档分级 + hysteresis + inhibit_rules）
- 6 份可执行 Runbook 脚本
- Active/Standby Controller（RTO ≤ 10 min, RPO ≤ 15 min）
- Chaos / GameDay 演练计划

### 🛠 平台集成
- CLI（`dlw`）+ Python SDK（同步 + 异步）
- HF cache 兼容（设 `HF_HOME` 透明走本系统）
- Webhook（task.completed / failed）
- MLflow Model Registry 自动注册
- K8s Operator + ModelDownload CRD
- 增量 / 差分下载（仅下变化文件）

---

## 仓库结构

```
modelpull/
├── docs/
│   ├── v2.0/                                    👈 当前设计权威
│   │   ├── 00-INDEX.md                          导航 + 角色阅读路径
│   │   ├── 01-architecture.md                   架构 / 状态机 / 数据模型
│   │   ├── 02-protocol.md                       API / 心跳 / WS 协议
│   │   ├── 03-distributed-correctness.md        Fence token / 恢复语义
│   │   ├── 04-security-and-tenancy.md           认证 / 租户 / 配额 / 合规
│   │   ├── 05-operations.md                     SLO / Runbook / 备份 / 灰度
│   │   ├── 06-platform-and-ecosystem.md         多源 / CLI / 集成 / Roadmap
│   │   ├── 07-test-plan.md                      ~450 测试矩阵
│   │   ├── 08-mvp-roadmap.md                    4 Phase 切片 + 任务分解
│   │   ├── 09-migration.md                      v1.x → v2.0 迁移
│   │   ├── 10-frontend-wireframes.md            9 个核心页面 wireframe
│   │   └── 11-cli-and-sdk-spec.md               dlw CLI + Python SDK 规范
│   └── archive/                                 v1.x 历史版本（已 superseded）
│
├── api/
│   └── openapi.yaml                             OpenAPI 3.1 完整 spec（可生成 SDK）
│
└── deploy/
    ├── helm/                                    Helm chart（生产就绪）
    │   ├── Chart.yaml + values.yaml
    │   └── templates/                           7 份 K8s 资源模板
    ├── prometheus/
    │   ├── recording-rules.yaml                 SLI + multi-burn-rate
    │   └── alerting-rules.yaml                  20+ 告警规则
    ├── alertmanager/
    │   └── routes.yaml                          PagerDuty/Slack/Jira 路由
    ├── grafana/
    │   ├── overview-dashboard.json
    │   └── slo-dashboard.json
    └── runbooks/scripts/                        6 个可执行 runbook 脚本
        ├── promote-standby.sh                   控制器故障切换
        ├── drain-executor.sh                    Executor 优雅排空
        ├── gc-orphan-parts.sh                   孤儿 .parts/ 清理
        ├── rotate-executor-mtls.sh              mTLS 证书轮换
        ├── verify-backup.sh                     夜间备份可恢复性验证
        └── maintenance.sh                       维护模式
```

---

## 谁应该读哪份

| 角色 | 推荐阅读路径 |
|------|------------|
| 👨‍💻 架构师 / 评审者 | `01` → `03` → `04` → `02` → `05` → `06` |
| 🔨 后端实现者 | `08` → `01` → `02` → `03` → `04` → `05` → `07` |
| 🧪 QA | `07` → `02` → `03` → `09` |
| 🛡️ 安全审计 | `04` → `02` → `01 §3` → `05 §10` |
| 🚨 SRE / on-call | `05` → `09` → `03 §3` → 部署物料 |
| 👤 用户 / 算法工程师 | `06 §5` (CLI/SDK) → `02 §1` |
| 🏗️ 平台 / 集成方 | `06` → `02` → `04 §1` → `api/openapi.yaml` |
| 📅 PM / Tech Lead | `08` → `07 §8` → `09` |
| 🎨 前端 | `10` → `api/openapi.yaml` |

入口：[`docs/v2.0/00-INDEX.md`](./docs/v2.0/00-INDEX.md)

---

## 设计亮点速读

### 1. 一键多源 = 测速 + LPT + 重平衡

```
任务创建
   ↓
并行测速（5 sources × 4 executors = 20 并发探测，软超时 8s）
   ↓
EWMA 融合（实测 0.7 + 历史 0.3）
   ↓
最优组合选择（不一定全用，引入慢源 +2% 协调开销惩罚）
   ↓
LPT 启发式 file-level 分配（最长任务先分给最快源）
   ↓
大文件（≥100MB）+ 多源 → chunk-level 并行
   ↓
下载中持续校准 → 退化触发局部重平衡
```

详见 [`06 §1.8`](./docs/v2.0/06-platform-and-ecosystem.md)。

### 2. Fence Token 防双发

v1.x 的 CAS 仅保护 DB 层，但内存队列 + 心跳响应 + 重连之间存在间隙：

```
T1: A 拿到 S（在内存队列）
T2: A 网络抖动失联
T3: controller 标 A faulty → reclaim S
T4: B 拿到 S 开始下载
T5: A 恢复后还在下载 S（不知道被 reclaim 了）
T6: A 完成 → controller 接受 → 双完成
```

v2.0 引入两层 fence：

- **Executor Epoch**：单调递增，每次 register +1，请求必须带当前 epoch
- **Assignment Token**：每次 assign 生成 fresh UUID，complete 时校验

详见 [`03 §2`](./docs/v2.0/03-distributed-correctness.md)。

### 3. 不变量驱动设计

14 条核心不变量（[`01 §7`](./docs/v2.0/01-architecture.md)），每条都有 CI 断言：

- HF 永远是 SHA256 真值来源
- HF Token 不离开 Controller
- Executor 不持长期 storage 凭证
- 业务表必须有 tenant_id
- ……

CI 强制失败任何违反不变量的 PR。

---

## Roadmap

| 版本 | 内容 |
|------|------|
| **v2.0**（设计完成） | 单租户 → 分布式 → 多租户 + 多源 → 生产加固，4 Phase / 13 周 |
| v2.1 | 跨地域复制 + SLA 分级 + 离线 export bundle + 行为遥测预热 |
| v2.2 | Active-active controller + Sigstore 验签 + 模型在线量化 + BLAKE3 流式哈希 |
| v2.3 | 多 controller cluster（按 tenant 分片）|

详见 [`08 §7`](./docs/v2.0/08-mvp-roadmap.md)。

---

## 为什么不直接用 huggingface_hub.snapshot_download？

| 维度 | huggingface_hub | modelpull |
|------|----------------|-----------|
| 单文件并发下载 | ⚠️ 受 hf_transfer 实验性限制 | ✅ DirectOffsetDownloader |
| 多机协调 | ❌ | ✅ |
| 多源加速 | ❌ | ✅ HF + ModelScope + Mirror + 自托管 |
| 断点续传跨进程 | ⚠️ 依赖文件名约定 | ✅ DB 持久化 + fence token |
| 多租户 / 配额 | ❌ | ✅ |
| Active/Standby | ❌ | ✅ |
| 可观测性 | ❌ | ✅ Prometheus + Grafana + OpenTelemetry |
| 审计 / 合规 | ❌ | ✅ 链式哈希审计日志 + License 策略 |
| Fence token 防双发 | N/A | ✅ |

如果你只是单机下一两个模型，`huggingface_hub.snapshot_download` 就够了。`modelpull` 是面向**团队 / 平台 / 多模型 / 大规模 / 国内多源加速**的场景。

---

## 现状声明

✅ **完成**：
- 18000+ 行设计文档 + 部署物料
- 完整 OpenAPI 3.1 spec（可生成 SDK）
- 5 位虚拟 reviewer 的 70+ 条问题已修复（架构一致性 / 分布式正确性 / 安全 / 运维 / 盲区）
- 4-Phase 13 周实施 roadmap
- v1.x → v2.0 数据迁移方案
- Helm chart + Prometheus 告警 + Grafana dashboard + 6 份 runbook 脚本

🚧 **待开始**：
- 后端代码实现（Python + FastAPI + SQLAlchemy）
- 前端代码实现（Vue 3 + Pinia + Element Plus）
- CLI / Python SDK 实现
- E2E 测试与 chaos 演练落地

---

## 贡献

设计阶段欢迎对架构 / 协议 / 不变量提出 review 意见。请通过 Issue 或 PR 提出。

实施开始后将开放代码贡献，遵循 [`07-test-plan.md`](./docs/v2.0/07-test-plan.md) 的覆盖率要求。

---

## 协议

[Apache License 2.0](./LICENSE)。

---

## 致谢

- HuggingFace 团队提供的 Hub API 和 huggingface_hub SDK
- ModelScope（魔搭）社区提供国内镜像
- hf-mirror.com 维护者提供社区镜像
- HuggingFace `hf_transfer` 项目启发了 DirectOffsetDownloader 设计
