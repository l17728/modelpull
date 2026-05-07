# Glossary — 术语与缩写表

> 解决 round 3 reviewer DOC-08：缩写如 LPT / EWMA / CUSUM 全文 127 次出现但无统一定义；中英概念混用（"分片" / "chunk" / "sub-chunk"）。本文是首次定义参考。

---

## 0. 命名约定（DOC-08）

- **同一概念优先用一个名字**：
  - "**chunk**" 表示 single file 内的 byte range（如 8 路 chunked download）
  - "**sub-chunk**" 表示 v2.1 子分片场景下 child subtask 的 chunk（与 parent 区分）
  - "**分片**" 是上面两者的中文，根据上下文区分
- **缩写首次出现**：必须配全称 + 链接到本文。例：`LPT (Longest Processing Time greedy)`
- **中英术语优先选英文**：API 字段、不变量、命令参数等机器可读 token 全英文；散文中可中英均可

---

## 1. 调度 / 算法

| 缩写 / 术语 | 全称 / 中文 | 含义 | 首次出现 |
|------------|----------|------|-------|
| **LPT** | Longest Processing Time | 调度启发式：先把最长任务分给最快机器 | 06 §1.6 |
| **EWMA** | Exponentially Weighted Moving Average | 指数加权移动平均；速度估计的平滑方法 | 06 §1.7 |
| **CUSUM** | Cumulative Sum Control Chart | 检测速度持续偏离基线的统计方法（替代硬阈值） | 13 §2.1 |
| **SPRT** | Sequential Probability Ratio Test | 序贯概率比检验；CUSUM 的兄弟方法 | 13 §2.1 |
| **MILP** | Mixed Integer Linear Programming | 混合整数线性规划；slow path 求解器 | 13 §1.2 |
| **LP relaxation** | Linear Programming relaxation | 去掉整数约束的 LP；anytime fallback 用 | 13 §9.1 |
| **DAG** | Directed Acyclic Graph | 有向无环图（不变量依赖、任务依赖） | INVARIANTS.md §6 |
| **makespan** | — | 一组任务全部完成所需的最长时间 | 13 §1.3 |
| **fence token** | — | 防止 stale write 的因果令牌（assignment_token + executor_epoch） | 03 §2 |
| **hysteresis** | 迟滞 | 触发阈值与解除阈值不对称，防抖动 | 13 §2.1 |
| **anytime algorithm** | — | 任何时间点都能返回当前 best feasible 解 | 13 §4.2 |

---

## 2. 网络 / 安全

| 缩写 / 术语 | 全称 / 中文 | 含义 | 首次出现 |
|------------|----------|------|-------|
| **mTLS** | Mutual TLS | 双向 TLS 认证（client 也要证书） | 04 §2.2 |
| **MCP** | Model Context Protocol | Anthropic 制定的 LLM 工具协议 | 12 §2.2 |
| **SSE** | Server-Sent Events | HTTP 长连流式推送（vs WebSocket 双向） | 02 §5 |
| **WSS** | WebSocket Secure | 加密 WebSocket（wss://） | 14 §1.2 |
| **STS** | Security Token Service | AWS 临时凭证签发服务 | 04 §3.2 |
| **OIDC** | OpenID Connect | 基于 OAuth 2.0 的身份认证标准 | 04 §1.2 |
| **JWT** | JSON Web Token | 紧凑型 token 格式 | 04 §2 |
| **HMAC** | Hash-based Message Authentication Code | 基于哈希的消息认证码 | 04 §2.2 |
| **NTLM** | NT LAN Manager | Windows 域 challenge-response 认证 | 14 §1.4 |
| **SPNEGO** | Simple and Protected GSS-API Negotiation | Kerberos / NTLM 协商封装 | 14 §1.4 |
| **CSI Driver** | Container Storage Interface Driver | K8s 标准存储/secret 接入接口 | 04 §3.4 |
| **WORM** | Write Once Read Many | 一次写多次读（合规存储类） | 04 §9 |
| **NFKC** | Normalization Form KC (Compatibility Composition) | Unicode 规范化（防 zero-width 攻击） | 12 §6.1 |
| **PSA** | Pod Security Admission | K8s pod 安全级别（restricted / baseline） | network-policy comments |
| **FQDN** | Fully Qualified Domain Name | 完整域名（如 `api.anthropic.com`） | network-policy comments |
| **PII** | Personally Identifiable Information | 个人身份信息（合规相关） | 12 §11.3 |

---

## 3. 存储 / 一致性

| 缩写 / 术语 | 全称 / 中文 | 含义 | 首次出现 |
|------------|----------|------|-------|
| **CAS** | Compare-And-Swap | 原子比较-替换操作 | 03 §2.3 |
| **CAS-then-enqueue** | — | 先原子 CAS 写 DB commit，再 enqueue 到响应；防双发模式 | 03 §2.3 |
| **multipart upload** | S3 分片上传 | 多 part 并发上传 + Complete 拼装 | 13 §5 |
| **upload_id** | — | S3 multipart 会话 ID | 13 §5.1 |
| **part_number** | — | S3 multipart 内的 part 序号（1-10000） | 13 §5.1 |
| **PartChecksumSHA256** | — | S3 multipart 内 part 的 sha256 校验值 | 03 §6.3 |
| **PG** | PostgreSQL | 主 DB | 全文 |
| **WAL** | Write-Ahead Log | PG 写前日志（备份依赖） | 05 §5.2 |
| **PITR** | Point-In-Time Recovery | 时间点恢复 | 05 §5 |
| **fsync** | — | OS 强制刷盘系统调用 | 03 §3 |
| **idempotency key** | 幂等 key | 客户端生成的请求唯一标识，防重试副作用 | 02 §2.3 |

---

## 4. 业务模型

| 术语 | 含义 | 首次出现 |
|------|------|-------|
| **chunk** | 单文件内的 byte range（multi-thread 并发下载单元） | 06 §1.6 |
| **sub-chunk** | v2.1 子分片场景下的 chunk；属 child subtask | 13 §5.2 |
| **subtask** | 一个文件的下载实体；可能是 single subtask（无 split）或 parent + N 个 child（split） | 01 §3.2 |
| **task** | 用户视角的下载任务；包含 1+ 文件（subtask）+ 元数据 | 01 §3.1 |
| **executor** | 下载执行器进程；多机部署 | 01 §5.2 |
| **executor_id** | 执行器全局唯一 ID（如 `host-12.local-worker-1`） | 01 §4.3 |
| **host_id** | 物理主机 ID；多 executor 同 host 共享 NIC | 01 §4.3 |
| **assignment_token** | fence token 的一种；每次 assign 生成 fresh UUID | 03 §2 |
| **executor_epoch** | fence token 的另一种；register 时单调递增 | 03 §2 |
| **optimization_generation** | PlanOptimizer 决策的 fence；每次 decide 时单调递增 | 13 §4.3.4 |
| **multipart_initiator_epoch** | controller standby 接管时的 fence | 13 §5.4 |
| **source** | 下载内容的来源（HF / ModelScope / mirror / 自托管） | 06 §1 |
| **ad-hoc source** | 任务级临时 source（v2.1）；vs `sources.yaml` 全局 source | 14 §6.0 |
| **storage backend** | 下载目的地（S3 / OBS / NFS / local） | 04 §3.2 |
| **probe leader** | 限速画像探测的集群级唯一执行者 | 14 §2.3 |
| **drain-purge 2-phase** | 凭证 alias 删除的两阶段协议 | 14 §3.6 |
| **canary** | probationary executor 的小流量验证模式 | 03 §5 |
| **paused_external** | source 全局降级时 subtask 进入的状态（不计 retry） | 03 §8 |
| **paused_disk_full** | 磁盘满时 subtask 进入的状态 | 03 §3.7 |
| **cancelling** | task / subtask 在用户取消后的中间态（保留已 verified 文件） | 03 §7 |

---

## 5. AI Copilot 专属

| 术语 | 含义 | 首次出现 |
|------|------|-------|
| **conversation** | 一次连续 AI 对话；含多 turn | 12 §5 |
| **turn** | conversation 内一次 user message + AI response 的循环 | 12 §6.1 |
| **tool call** | AI 触发的内部工具调用（read-only 或 write） | 12 §3 |
| **T1 (trust level 1)** | 可信结构化外部 metadata（HF API JSON：license/sha/file list） | 12 §3.3 |
| **T2 (trust level 2)** | 用户内容（README / paper / github file body）；高危注入向量 | 12 §3.3 |
| **modified_input** | 用户对 AI 提议的写工具参数做修改后再 confirm | 12 §4.2 |
| **rejected confirm** | 用户拒绝 AI 提议的写工具调用 | 12 §6.3 |
| **prompt injection** | 通过外部内容操纵 LLM 行为的攻击（直接 / 间接 / multi-turn） | 12 §6.1 |
| **confusables** | 视觉相似但 Unicode 不同的字符（如 `Іgnore` 西里尔 І） | 12 §6.1 |

---

## 6. RFC 2119 关键字（DOC-09）

> modelpull v2.0 文档全部采用 RFC 2119 风格表达规范性。CI 在未来版本会 lint 检查："应该 / 推荐 / 最好" 等弱化词不被混入。

| 关键字（中文） | 关键字（RFC 2119） | 强度 | 用法 |
|------------|------------------|------|------|
| **必须** | **MUST** | 绝对要求 | 不变量 / 核心安全约束 / 数据完整性 |
| **不得 / 禁止** | **MUST NOT** | 绝对禁止 | 反模式 / 安全禁区 |
| **应当** | **SHOULD** | 强烈推荐但允许例外 | 性能 / 可观测 / 工程实践 |
| **不应** | **SHOULD NOT** | 不推荐但允许例外 | 反模式（弱） |
| **可以** | **MAY** | 可选 | 可配置 / 优化 |

**禁止使用的弱化词**：

- ❌ "应该" → 用 SHOULD（应当）
- ❌ "推荐" → 用 SHOULD（应当）
- ❌ "最好" → 用 SHOULD（应当）
- ❌ "可能" → 用 MAY（可以）/ MIGHT（可能；仅描述性，不带规范）

CI lint：未来版本 `tools/lint_rfc2119.py` 会扫描"应该 / 推荐 / 最好" 等并 fail PR。

---

## 7. 与其他文档

- 权威定义见各术语"首次出现"列指向章节
- 不变量索引：[`INVARIANTS.md`](./INVARIANTS.md)
- 入门：[`00-OVERVIEW.md`](./00-OVERVIEW.md)
- 全文导航：[`00-INDEX.md`](./00-INDEX.md)
