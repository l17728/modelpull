# modelpull 用户手册

> 版本：v2.1 / 2026-05  
> 本手册随软件一同部署，AI 助手可直接读取本文件获取帮助内容。

---

## 目录

1. [系统概述](#系统概述)
2. [设计思路](#设计思路)
3. [核心概念](#核心概念)
4. [快速开始](#快速开始)
5. [功能详解](#功能详解)
   - [下载任务](#下载任务)
   - [执行器管理](#执行器管理)
   - [配额管理](#配额管理)
   - [审计日志](#审计日志)
   - [AI 助手](#ai-助手)
6. [进阶功能](#进阶功能)
7. [常见问题](#常见问题)

---

## 系统概述

**modelpull** 是一个面向团队的**大模型权重分布式下载管理平台**。它解决的核心问题是：

- 团队中多人需要下载相同的模型文件（如 LLaMA、Qwen、Stable Diffusion），每次都要重复等待数十 GB 的下载。
- 模型托管在 HuggingFace、ModelScope 等平台，国内访问速度不稳定，需要多源切换。
- 下载完的模型权重应该集中存储、去重复用，而不是每台机器各自保存一份。

**modelpull 的价值主张：**

> 提交一个下载任务 → 系统自动找最快的源 → 多个执行器协同下载 → 存储去重后挂载给所有用户

---

## 设计思路

### 分层架构

```
浏览器 / CLI / SDK
        ↓
   控制器（Controller）
    FastAPI REST + SSE
        ↓
   PostgreSQL 数据库
        ↓
执行器集群（Executor Pool）
   实际执行下载 I/O
        ↓
  存储后端（Storage Backend）
   本地磁盘 / NFS / S3
```

**控制器** 是唯一的控制平面。它接受任务请求、调度分块、协调执行器，自身不做任何文件 I/O。

**执行器** 是无状态的工作节点。每个执行器向控制器注册自己、领取分片任务、汇报进度。执行器崩溃后任务自动被其他执行器接管（恢复机制）。

**存储后端** 保存最终的模型文件。同一个文件哈希只存一份（物理去重），多个任务可以引用同一份物理文件。

### 安全模型

- **多租户隔离**：每个请求携带 JWT，包含 `tenant_id`。所有数据库查询强制过滤 `tenant_id`，租户之间数据完全隔离。
- **执行器认证**：执行器通过 mTLS 证书 + JWT 双重认证，防止伪造心跳。
- **操作审计**：所有写操作（创建/取消任务、AI 工具调用）记录到审计日志，不可篡改。
- **HF 令牌保护**：HuggingFace API Token 仅保存在控制器端，通过内部反向代理转发给执行器，执行器永远不直接持有令牌。

### 高可用

- **主备切换**（Active/Standby）：两个控制器实例争抢 PostgreSQL 咨询锁。持有锁的为主节点，负责调度和扫描；备节点随时待命，主节点宕机后数秒内自动接管。
- **任务恢复**：主节点启动时扫描所有 `running` 状态的子任务，超时未心跳的重置为 `pending` 重新调度。
- **增量下载**：同一模型的多次下载任务自动复用已完成的分片，不重复传输已有数据。

---

## 核心概念

### 下载任务（Task）

一次"下载某个模型到某个存储位置"的请求就是一个**任务**。

| 状态 | 含义 |
|------|------|
| `pending` | 等待调度，尚未分配执行器 |
| `running` | 正在下载，至少有一个分片在进行 |
| `completed` | 所有分片下载完成 |
| `failed` | 遇到不可恢复的错误 |
| `cancelled` | 用户主动取消 |
| `paused_external` | 外部暂停（如配额耗尽） |

### 子任务与分片（Subtask / Chunk）

每个任务按文件分解为**子任务**，每个子任务按字节范围分解为**分片**。分片是最小的执行单元，可以并发下载、独立恢复。

```
任务: 下载 Qwen2-7B
├── 子任务: config.json (1 片)
├── 子任务: model-00001-of-00004.safetensors (128 片)
├── 子任务: model-00002-of-00004.safetensors (128 片)
└── ...
```

### 执行器（Executor）

执行器是独立进程，可以部署在任意有网络访问权限的机器上。执行器：

- 启动时向控制器注册，获得证书
- 每 30 秒发送心跳汇报状态
- 领取分片任务，下载完成后校验 SHA256，汇报结果

执行器数量越多，并行下载速度越快。

### 存储后端（Storage Backend）

存储后端是模型文件最终保存的地方。目前支持：

- **本地磁盘**：直接挂载到执行器所在机器
- **NFS 共享存储**：多个执行器共享同一个挂载点，下载完成自动去重
- **S3 兼容对象存储**：支持 AWS S3、MinIO 等

### 配额（Quota）

每个租户有月度下载量上限。超过配额后：

- 新任务无法创建
- 正在运行的任务暂停（状态变为 `paused_external`）
- 管理员可以调整配额上限

---

## 快速开始

### 示例：下载 HuggingFace 上的模型

**第一步：登录系统**

打开浏览器访问系统地址，使用团队账号登录。首次使用请联系管理员获取账号。

---

**第二步：新建下载任务**

1. 点击左侧导航栏「**任务**」
2. 点击右上角「**新建任务**」按钮
3. 填写模型仓库地址，例如：`Qwen/Qwen2-7B-Instruct`
4. 选择目标存储位置（由管理员预先配置）
5. 点击「**提交**」

提交后任务进入 `pending` 状态，等待调度器分配执行器。

---

**第三步：监控下载进度**

点击任务列表中的任务名称，进入「**任务详情**」页面。

任务详情页面提供实时进度：

- **速度 / 预计完成时间**：基于最近 30 秒的平均速度计算
- **分片环形图**：直观展示各执行器的下载进度
- **来源分配**：各下载源（HuggingFace、镜像站、ModelScope）的贡献比例
- **执行器泳道图**：哪台机器在下载哪些文件
- **事件日志**：任务生命周期中的所有重要事件

---

**第四步：使用 AI 助手查询状态**

点击右上角「🤖 AI 助手」，在对话框中用自然语言提问：

- `我有哪些正在运行的任务？`
- `查询 Qwen2-7B 的下载进度`
- `取消任务 xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`（取消前需确认）

---

### 示例：通过命令行工具下载

如果你更习惯命令行，可以使用内置的 `dlw` CLI：

```bash
# 登录（设备流程，浏览器扫码授权）
dlw login

# 提交下载任务
dlw submit Qwen/Qwen2-7B-Instruct --storage-id 1

# 查看任务列表
dlw task list

# 实时监控进度（SSE 流）
dlw task watch <task-id>

# 取消任务
dlw task cancel <task-id>
```

---

## 功能详解

### 下载任务

#### 创建任务

**必填字段：**

| 字段 | 说明 | 示例 |
|------|------|------|
| 仓库 ID | HuggingFace/ModelScope 的 `用户名/模型名` | `Qwen/Qwen2-7B-Instruct` |
| 存储后端 | 目标存储位置的 ID | 由管理员告知 |
| 版本（可选） | Git commit SHA 或 branch，默认 `main` | `main` |

#### 任务状态流转

```
pending → running → completed
           ↓            
         failed   
           ↓
     paused_external (配额耗尽时)
           ↓
     cancelled (用户取消)
```

#### 取消任务

在任务详情页点击「**取消**」按钮，或通过 AI 助手发送取消指令。已完成的任务无法取消。

#### 分片级进度

任务详情页的「**下载分片**」选项卡展示每个文件的每个 128MB 分片的下载状态：

- 🟢 **已完成**：分片已下载并通过 SHA256 校验
- 🔵 **下载中**：当前正由某个执行器下载
- ⚪ **待处理**：尚未分配
- 🔴 **失败**：校验失败或网络错误，将自动重试

---

### 执行器管理

执行器页面（导航栏「**执行器**」）展示所有已注册的执行器节点：

| 字段 | 说明 |
|------|------|
| 执行器 ID | 唯一标识符 |
| 状态 | `active`（正常）/ `inactive`（离线）/ `draining`（排空中）|
| 最后心跳 | 距上次心跳的时间，超过 90 秒视为离线 |
| 当前并发数 | 正在执行的分片数量 |
| 存储路径 | 执行器挂载的存储路径 |

**执行器离线后的行为：**

控制器的恢复扫描（每 30 秒一次）会检测到超时的心跳，将该执行器正在处理的分片重新标记为 `pending`，由其他在线的执行器接管。

---

### 配额管理

配额页面（导航栏「**配额**」）展示当前租户的用量：

| 指标 | 说明 |
|------|------|
| 本月已下载 | 当前自然月内成功下载的总字节数 |
| 配额上限 | 管理员设置的月度上限（字节） |
| 剩余配额 | 上限 - 已用量 |
| AI Token 用量 | 本月 AI 助手消耗的 Token 数 |

**配额超限处理：**

1. 下载配额耗尽 → 正在运行的任务暂停为 `paused_external`，新任务无法创建
2. AI Token 配额耗尽 → AI 助手返回「配额已用完」提示，不影响下载功能

联系管理员可以申请提高配额上限。

---

### SLA 服务等级（v2.1）

每个租户有一个 **SLA 等级**（sla_tier），决定调度优先级和资源准入：

| 等级 | 调度权重 | 准入控制 | 适用场景 |
|------|---------|---------|---------|
| `critical` | ×4（优先） | 永不被拒 | 生产关键业务 |
| `standard` | ×2 | 系统繁忙 > 99% 时拒 | 普通业务（默认） |
| `bulk` | ×1（最低） | 系统繁忙 > 90% 时拒 | 批量回灌、离线分析 |

**查看自己的等级**：「设置」页面会显示当前租户的 sla_tier。

**修改等级**：仅 `system_admin` 可改，所有变更进审计日志：
- UI：管理员在「设置」页面下拉选择
- REST：`PUT /api/v1/tenants/{id}/sla` body `{"sla_tier": "critical"}`

**饿死保护**：bulk 等级的任务如果等待超过 30 分钟，调度权重自动 ×2 强制上浮，避免被永远抢占。

---

### 审计日志

审计日志页面（导航栏「**审计**」）记录所有操作行为：

| 字段 | 说明 |
|------|------|
| 时间 | 操作发生的精确时间 |
| 操作者 | 执行操作的用户 ID |
| 动作 | 如 `task.create`、`task.cancel`、`ai.tool.dlw_cancel_task` |
| 资源类型 | 被操作的资源类型 |
| 结果 | `success` / `error` |
| 载荷 | 操作的详细参数（隐去敏感信息） |

审计日志支持按时间范围、动作类型、操作者过滤和关键词搜索。**日志不可修改，永久保留。**

---

### AI 助手

AI 助手（右上角「🤖 AI 助手」）是集成在界面中的智能对话助手。打开抽屉后顶部
的 **🛠 可用工具** 面板（默认展开）会列出所有可调用工具及示例提问。

**优先级顺序**：AI 会根据你的问题先尝试领域专用工具，再用 web 兜底，最后才回退
到模型记忆 — 每条回复底部的灰色徽章告诉你这次答案的真实来源（💭 来自模型记忆 /
🤗 来自 Hugging Face / 🔍 来自 ModelScope 等），方便区分真实查询和模型幻觉。

**查询类（11 个，只读，无需确认）：**

| 工具 | 用途 |
|------|------|
| `dlw_list_tasks` | 列出我的任务（可按状态过滤） |
| `dlw_get_task` | 按 id 查单个任务详情 |
| `dlw_get_task_events` | 查任务的最近事件（状态变更 / 错误） |
| `dlw_quota_current` | 查租户当前配额用量 |
| `dlw_list_storages` | 列出可用存储后端（创建任务前先查） |
| `hf_api_metadata` | HF：查仓库元数据（sha / 文件列表） |
| `hf_model_card` | HF：拉取仓库 README / 模型卡 |
| `search_huggingface_models` | 在 huggingface.co 按关键词搜模型 |
| `search_modelscope_models` | 在 modelscope.cn 按关键词搜模型 |
| `web_search` | Brave 网页搜索（需 `DLW_AI_WEB_SEARCH_API_KEY`） |
| `fetch_user_content` | 抓取允许列表内的 HTTPS URL 内容（需管理员启用） |

**操作类（10 个，每次都需要二次确认）：**

| 工具 | 用途 | 权限 |
|------|------|------|
| `dlw_create_task` | 新建下载任务（revision 默认 main、storage 自动选默认） | 用户 |
| `dlw_cancel_task` | 取消运行中任务 | 用户 |
| `dlw_delete_task` | 删除终态任务，释放存储+配额 | 用户 |
| `dlw_retry_task` | 用相同参数新建任务（语义化重下） | 用户 |
| `dlw_upgrade_task` | 升级到新 revision，未变文件自动 inherit | 用户 |
| `dlw_patch_task` | 改运行中任务的 priority / source_strategy / source_blacklist | 用户 |
| `dlw_create_local_user` | 新建本地用户 | system_admin |
| `dlw_reset_local_password` | 重置本地用户密码 | system_admin |
| `dlw_set_tenant_quota` | 设置租户配额上限，审计记录 | system_admin |
| `dlw_create_replication` (v2.1) | 创建跨地域复制任务（object → storage） | system_admin |

**决策链全程透明**：助手回复上方会按时序显示「决策链」面板 — 每次思考、每次工具
调用都打印出来（工具名、输入参数、返回结果摘要），点击可展开查看完整 JSON。让你
能验证"AI 真的查了 HF API"还是"AI 自己编了答案"。

**使用技巧：**

- 直接说自然语言："Hugging Face 上最新的 deepseek 是什么"
- 任务 UUID 可以粘贴："任务 abc12345 为什么失败？"
- 一句话端到端："下载 deepseek-ai/DeepSeek-R1" → AI 会先搜 HF → 弹确认 → 创建任务
- 操作确认卡片可修改 AI 建议的参数后再点确认
- 每次对话上下文保留，可以追问

**AI 助手的安全机制：**

- 租户隔离 — AI 只能访问你有权限看的数据，跨租户调用返回 404
- 审计 — 所有 AI 工具调用记录在审计日志（`ai.tool.*` action）
- 二次确认 — 所有写操作必须用户点「确认」才执行，AI 不能直接写
- T2 净化 — 外部内容（模型卡 / 搜索结果 / URL 抓取）经严格净化，无法注入恶意指令
- system_admin 检查 — 管理员级工具（建用户 / 改配额）在工具入口和 chat service
  双层检查（defense-in-depth）

---

## 进阶功能

### 多源下载加速

系统支持同时从多个来源下载同一个模型文件，自动选择速度最快的源：

- **HuggingFace 官方**：`https://huggingface.co`
- **HuggingFace 镜像**：如 `https://hf-mirror.com`（国内访问更快）
- **ModelScope**：阿里云模型平台，部分国产模型仅在此托管

管理员可以在 `config/sources.yaml` 中配置可用的源及优先级。系统会自动测速，为每个分片选择当前最快的源，并在源出现错误时自动切换（黑名单机制，60 分钟后自动恢复）。

### 增量下载

如果同一个模型有多个下载任务（如不同版本），系统会自动检测已完成的文件分片并复用，跳过已下载的内容。

例如：第一次下载 `Qwen2-7B-Instruct` 的 `main` 分支，第二次下载同一模型的新 commit，如果大部分文件未变动，系统只下载变化的分片。

### 物理去重

存储层按文件内容哈希（SHA256）进行物理去重。即使两个不同的任务下载了同一个文件（哈希相同），存储上只保留一份物理文件，节省磁盘空间。

### Physical GC（v2.1，管理员）

去重保留的物理文件在所有引用任务被删除后会变为 **tombstone**（引用计数为 0），但默认不会立即从存储中真正删除 — 留给运维一个手动反悔窗口。

**Physical GC** 是一个后台任务，可由管理员手动触发，会：

1. **Phase 1（tombstone 清理）**：扫描所有 `refcount = 0` 且 `created_at < cutoff` 的物理键，调用存储驱动真正 unlink/DeleteObject。
2. **Phase 2（LRU 驱逐）**：如果某租户的存储用量超过 90% × `quota_storage_gb`，按 `last_referenced_at` 最旧优先选取候选并驱逐到 90% 以下。

**启用与触发**（仅 `system_admin`）：

```bash
# 全局开关（默认关闭，避免误删）
export DLW_PHYSICAL_GC_ENABLED=true

# 手动触发一次 GC
curl -X POST -H "Authorization: Bearer $TOKEN" \
     http://controller:8001/api/v1/admin/gc/run

# 仅清理某个租户
curl -X POST -H "Authorization: Bearer $TOKEN" \
     -d '{"tenant_id": 5}' \
     http://controller:8001/api/v1/admin/gc/run

# 查看上次执行结果
curl -H "Authorization: Bearer $TOKEN" \
     http://controller:8001/api/v1/admin/gc/status
```

每次驱逐都写审计日志（action=`physical_gc.evict`），包含对象 ID / 大小 / 原因。

### 跨地域复制（v2.1 ✅ 已落地）

支持把某个 `storage_object` 复制到另一个 `storage_backend`（典型场景：把上海集群下载好的 model 推到深圳的 S3 桶）。REST、Web UI（`/replication`）、AI 助手三种入口均可用（见本节末）。

```bash
# 创建复制任务（任何认证用户均可，仅限本租户的对象）
curl -X POST -H "Authorization: Bearer $TOKEN" \
     -d '{"source_object_id": 123, "target_storage_id": 7}' \
     http://controller:8001/api/v1/replication

# 列出本租户的所有复制任务
curl -H "Authorization: Bearer $TOKEN" \
     "http://controller:8001/api/v1/replication?status=pending"

# 取消尚未跑完的任务
curl -X POST -H "Authorization: Bearer $TOKEN" \
     http://controller:8001/api/v1/replication/1/cancel
```

**当前状态**：Sprint 4 + 5 + 6 已完成 — 数据模型 + REST + worker + 前端 UI + Prometheus + AI 工具。

**Web UI**：admin / tenant_admin 用户左侧侧边栏可看到「跨地域复制」入口（`/replication`），支持列表 + 状态筛选 + 创建 + 取消。

**AI 助手**：通过自然语言"把 object 123 复制到 storage 7"会触发 `dlw_create_replication` 写工具（system_admin only），二次确认后入队。

**监控**：
- Prometheus scrape endpoint `/metrics` 暴露 3 个指标：
  - `dlw_replication_bytes_total{tenant_id,target_storage_id,status}` Counter
  - `dlw_replication_jobs_total{tenant_id,status}` Counter
  - `dlw_replication_job_duration_seconds{status}` Histogram
- Grafana dashboard：`deploy/grafana/replication-throughput.json`（吞吐 / 成功率 / p50-p95 / 按 target 分组）

**启用后台 worker**（仅 system_admin 配置）：

```bash
# 开启 worker（默认关闭，避免在没有真 S3 target 时空转）
export DLW_REPLICATION_WORKER_ENABLED=true
# 可选：每 tenant 限速（默认 100 MB/s，decimal MB）
export DLW_REPLICATION_BANDWIDTH_MBPS=50
# 可选：worker 轮询间隔（默认 5 秒）
export DLW_REPLICATION_WORKER_POLL_INTERVAL_SECONDS=10
```

worker 启用后会自动从 `pending` 队列取任务执行：
1. 同 sha 已在 target → `skipped_existing`
2. 流式 read source → 内存 buffer → put target，每 chunk 更新 `bytes_transferred`
3. 完成后插入 target StorageObject 行（dedup 唯一约束自动处理并发竞争）
4. 失败重试 3 次（指数退避 1s / 2s），sha 校验失败不重试
5. 中途取消（`POST /api/v1/replication/{id}/cancel`）：下次 chunk 进度回调会感知并立即停止，不写入 target

### 命令行 SDK

如果需要在脚本或 CI/CD 中集成，可以使用 Python SDK：

```python
import asyncio
from dlw.sdk import AsyncClient

async def main():
    async with AsyncClient(base_url="http://your-server:8001",
                           token="your-jwt-token") as client:
        # 提交任务
        task = await client.tasks.create(repo_id="Qwen/Qwen2-7B", storage_id=1)
        print(f"Task created: {task.id}")

        # 实时监控
        async for event in client.tasks.stream(task.id):
            print(event)

asyncio.run(main())
```

---

## 常见问题

### Q：任务一直是 pending 状态怎么办？

**A：** 可能的原因：
1. 没有在线的执行器。检查「执行器」页面，确认至少有一个 `active` 状态的执行器。
2. 配额已耗尽。检查「配额」页面，查看剩余配额。
3. 存储后端已满。联系管理员检查磁盘用量。

---

### Q：下载速度很慢怎么办？

**A：** 
1. 检查是否配置了国内镜像源（`hf-mirror.com`）。
2. 增加执行器数量——更多执行器意味着更多并行下载连接。
3. 检查「来源分配」图表，看是否有某个来源持续失败导致切换。

---

### Q：执行器显示 inactive 怎么回复？

**A：** 重启执行器进程。执行器重启后会自动重新注册并恢复工作，无需手动操作。执行器之前正在处理的分片会在约 90 秒后被控制器重新分配给其他执行器。

---

### Q：AI 助手说"操作被取消"是什么意思？

**A：** AI 建议的写操作（如取消任务）在你点击确认卡片的「拒绝」按钮时，或者会话超时时，都会显示此消息。这不是错误，只是表示操作被跳过了。

---

### Q：如何为新用户开通账号？

**A：** 目前支持两种方式：
1. **设备流程**（推荐）：用户在登录页点击「设备授权登录」，获得一个授权码，管理员在管理后台批准。
2. **JWT 直接颁发**（开发/测试环境）：管理员使用 `DLW_AUTH_DEV_MODE=true` 模式，通过 `issue_system_jwt` 工具生成 JWT，分发给用户。

---

### Q：AI 助手可以访问互联网吗？

**A：** 默认关闭。管理员设置 `DLW_AI_WEB_SEARCH_ENABLED=true` 并配置 `DLW_AI_WEB_SEARCH_API_KEY`（Brave Search API 免费密钥）后启用。启用后 AI 助手可以搜索互联网获取最新信息，搜索结果经过安全净化处理。

---

### Q：如何备份数据？

**A：** 核心数据都在 PostgreSQL 中。定期备份数据库即可恢复所有任务记录、执行器注册信息和审计日志。模型文件本身在存储后端，需要单独备份存储路径。

---

## v2.1 高级管理（系统管理员）

以下都是 admin-only 配置，通过环境变量启停，默认全关。完整启用顺序见 `docs/operator/v21-production-deployment.md` § "Deploy step 4 — Feature flags"。

### 自适应优化（Sprint 7-9）

调度器可以基于历史样本周期重规划 pending chunk 的来源，缩短整体 makespan：

```bash
# Master switch（loop 才跑）
export DLW_ADAPTIVE_OPTIMIZER_ENABLED=true

# Shadow 模式（仅 log + Prometheus metric，不真改 source_id）
# 推荐先用 shadow 跑 1-2 周，对照 metric 看效果
unset DLW_ADAPTIVE_OPTIMIZER_APPLY   # 默认 false

# 真应用（写库）— 仅在 shadow 验证 OK 后启用
export DLW_ADAPTIVE_OPTIMIZER_APPLY=true
```

监控指标：`dlw_optimizer_solve_duration_seconds` (Histogram) + `dlw_replan_chunk_moves_total{mode=shadow|apply}` (Counter)。

### 反向 WSS 企业内网部署（Sprint 10-13）

让位于内网防火墙后的 executor 主动连出到 controller，无需开 inbound 端口。Executor 端在 v2.1 client 启用 WSS dialing 即可（与现有 mTLS + JWT 一致）。

Admin 操作：

```bash
# 查看当前所有 reverse-WSS 连接
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
     http://controller:8001/api/v1/admin/reverse-ws/sessions

# 给 executor 发送白名单命令（status / drain / restart）
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" \
     -d '{"command": "drain"}' \
     http://controller:8001/api/v1/admin/executors/ex-1/command
```

### 凭证池 envelope encryption（Sprint 12）

`storage_backends.config_encrypted` 列可以从 Sprint 12 起被 Fernet 加密。新行自动加 magic 前缀 + 包裹；旧 plaintext 行无感继续工作（非破坏性升级）。

```bash
# 生成 Fernet key（base64 url-safe，32 字节）
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# 写入 controller secret store
export DLW_CONFIG_KEY=<上面输出>

# Key rotation：换 key 后旧加密行解密会抛 _CryptoError —— 必须先 batch
# 重加密所有旧行（follow-on tool TBD），再切 key。
```

---

*本手册最后更新：2026-05-27（v2.1 Sprint 1/3/4/5/6/7-9/10-13 全部 ship）。如有问题请通过 AI 助手提问，或联系系统管理员。*
