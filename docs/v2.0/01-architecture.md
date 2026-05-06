# 01 — 架构与单一真相

> 角色：所有人入门的第一份；建立单一权威定义。
> 取代：v1.0 §1 §4 §12，v1.4 §1 §4.1 §8.6 §11，v1.5 §3。

---

## 0. 从历史文档迁移指引

| 旧位置 | v2.0 位置 |
|--------|----------|
| `design_document.md` §1 项目背景 | 本文 §1 |
| `design_document.md` §4 系统架构 | 本文 §2 |
| `design_document.md` §5.1.1 数据模型 | 本文 §4 |
| `design_document.md` §12 技术选型 | 本文 §6 |
| `design_document_fault_tolerance_and_visualization.md` §1.1 节点状态机 | 本文 §3.3 |
| `design_document_fault_tolerance_and_visualization.md` §4.1 任务状态机（旧） | 已废弃，见 §3.1 |
| `design_document_fault_tolerance_and_visualization.md` §8.6 任务状态机（新） | 已合并到 §3.1 |
| `design_document_review_and_e2e.md` §3 多执行器 | 本文 §5.3 + 06 §0 |

---

## 1. 项目背景与目标

### 1.1 背景

当前大语言模型权重文件规模巨大：

| 模型 | 参数量 | 总大小 | 分片数 | 单分片大小 |
|------|--------|--------|--------|-----------|
| DeepSeek-V3 (FP8) | 671B | 689 GB | 163 | ~4.3 GB |
| Kimi-K2-Instruct (FP8) | ~1T | 1.03 TB | 61 | ~17.1 GB |
| Qwen3-72B-Instruct (BF16) | 72B | 144 GB | 30 | ~4.8 GB |
| GLM-4-9b-Chat (BF16) | 18.6B | 18.5 GB | 10 | ~1.9 GB |

单机下载 TB 级模型耗时极长，需要分布式下载方案。

### 1.2 目标（v2.0）

| 目标 | 量化指标 |
|------|---------|
| 多机并行下载 | ≥ 10 台 Executor 并行，单任务带宽 ≥ 链路带宽总和 60% |
| 断点续传 | 任意节点崩溃 / 网络中断后，已下载的字节不重下 |
| 全速利用带宽 | 单 Executor 多线程，单文件支持 8-16 chunk 并行 |
| 动态扩缩容 | Executor 注册/退出对在跑任务无影响 |
| 完整性保证 | 端到端 SHA256；可证伪：任何 commit 一致性破坏立即 fail-stop |
| **多租户**（v2.0 新增） | 租户隔离、配额、计量 |
| **生产可运维**（v2.0 新增） | SLO 99.9% 任务完成率；on-call 有 runbook；RPO=15min RTO=10min |

### 1.3 非目标

- 不做 inference / serving（这是模型推理框架的事）
- 不做用户层模型管理（如版本对比、AB 测试）
- 不做训练（仅下载到本地或对象存储）

---

## 2. 系统架构

### 2.1 总体架构图

```
                    ┌──────────────────────────────────────────────────┐
                    │                Web UI (Browser)                    │
                    │     仅渲染 + 用户交互；不做 HF API；不做调度决策    │
                    └────────────────┬─────────────────────────────────┘
                                     │ OIDC + JWT
                                     │ HTTPS REST + WSS
                                     ▼
            ┌────────────────────────────────────────────────────────────┐
            │                    Controller (Active)                      │
            │  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
            │  │ HF Proxy     │  │ Scheduler    │  │ Tenant / Quota   │  │
            │  │ (token sink) │  │ (CAS+epoch)  │  │ Manager          │  │
            │  └──────────────┘  └──────────────┘  └──────────────────┘  │
            │  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
            │  │ Heartbeat    │  │ State Mgr    │  │ Probe / Health   │  │
            │  │ Aggregator   │  │ (recovery)   │  │ Monitor          │  │
            │  └──────────────┘  └──────────────┘  └──────────────────┘  │
            │  ┌──────────────────────────────────────────────────────┐  │
            │  │ PostgreSQL (RPO=15min, WAL archive + basebackup)    │  │
            │  └──────────────────────────────────────────────────────┘  │
            └─────────┬───────────────────────────────────┬──────────────┘
                      │ standby replication              │
                      ▼                                  │
              ┌──────────────────┐                       │
              │ Controller       │                       │ mTLS
              │ (Standby)        │                       │ + Executor JWT (TTL=1h)
              └──────────────────┘                       │
                                                         │
        ┌────────────────────────┬────────────────────────────────────┐
        │                        │                                    │
        ▼                        ▼                                    ▼
   ┌────────────┐          ┌────────────┐                    ┌────────────┐
   │ Executor 1 │          │ Executor 2 │       ......       │ Executor N │
   │            │          │            │                    │            │
   │ • DirectOffsetDownloader (单文件多线程并发写)               │
   │ • .parts/ 持久卷（不放 /tmp）                                │
   │ • multipart upload_id 持久化                                 │
   │ • Executor JWT 短 TTL，过期主动续签                          │
   └─────┬──────┘          └─────┬──────┘                    └─────┬──────┘
         │                       │                                  │
         ▼                       ▼                                  ▼
       (HF reverse-proxy via Controller, executor 不直连 HF)
         │                       │                                  │
         └────┬──────────────────┴──────────────────────────────────┘
              │
              ▼
    ┌────────────────────────────────────────────────────┐
    │     Storage Backend (S3 / OBS / MinIO / NFS)        │
    │  Executor 用 STS 临时凭证（不持长期 AK/SK）          │
    └────────────────────────────────────────────────────┘
```

### 2.2 三个最重要的边界变化（相对 v1.x）

🔒 **不变量 1：UI 永不直连 HuggingFace API**
v1.0 中 UI 与 Controller 都可能调 HF API，安全模型不清晰。v2.0 只允许 Controller 调用，UI 仅通过 `/api/models/search` 间接搜索。

🔒 **不变量 2：HF Token 不离开 Controller**
v1.0 中 HF Token 在心跳响应里下发到 Executor。v2.0 中 Controller 作为 reverse-proxy（详见 04-security §3.1），Executor 通过 `https://controller/hf-proxy/...` 走代理，永不持有 HF Token 明文。

🔒 **不变量 3：Executor 不直接持有长期对象存储凭证**
S3 / OBS 凭证由 Controller 用 `sts:AssumeRole` 换为 1h TTL 临时凭证下发，过期主动续签。

### 2.3 通信方向

| 方向 | 协议 | 频率 | 鉴权 |
|------|------|------|------|
| UI → Controller | HTTPS REST | 用户交互触发 | OIDC + JWT |
| UI ← Controller | WSS | 进度推送（snapshot+delta+seq） | JWT 子协议握手 |
| Executor → Controller | HTTPS（心跳） | 10s | mTLS + Executor JWT + HMAC body |
| Executor ← Controller | 心跳响应（任务下发） | 10s | （同上） |
| Executor → HF | HTTPS via Controller proxy | 下载触发 | Controller 注入 HF Token |
| Executor → Storage | HTTPS（S3 multipart） | 上传触发 | STS 临时凭证 |
| Controller → Standby | PG streaming replication | 持续 | TLS 证书 |

🔒 **不变量 4：Controller 不主动反向连接 Executor**
旧设计中 ProbeScheduler 直接 GET executor 的 `/health`，要求 Executor 暴露端口。v2.0 健康判定纯靠心跳缺失检测，Executor 无需公网端口。

---

## 3. 权威状态机

> ⚠️ 旧 v1.0 §5 / v1.4 §4.1 / v1.4 §8.6 中的状态机图全部废弃。
> ⚠️ 实现 / E2E / UI 三方所引用的"合法状态集合"必须以本节为唯一来源。

### 3.1 任务状态机（DownloadTask.status）

```
       create
         │
         ▼
   ┌───────────┐
   │  pending  │◄────── retry 后回退
   └─────┬─────┘
         │ scheduler 分配 ≥1 个 subtask
         ▼
   ┌───────────┐
   │ scheduling│  ─── 用户 cancel ──┐
   └─────┬─────┘                    │
         │ 至少一个 subtask 进入 downloading
         ▼                          │
   ┌────────────┐                   │
   │ downloading│ ── 用户 cancel ──►│
   └─────┬──────┘                   │
         │ 全部 subtask completed   │
         ▼                          │
   ┌────────────┐                   │
   │ verifying  │ ─── 校验失败 ────►│ failed
   └─────┬──────┘                   │
         │ 全部 SHA256 + 远端 size  │
         ▼                          │
   ┌────────────┐                   │
   │ completed  │ (terminal)        │
   └────────────┘                   ▼
                              ┌────────────┐
                              │ cancelling │
                              └─────┬──────┘
                                    │ 所有 in-flight 处置完
                                    ▼
                              ┌────────────┐
                              │ cancelled  │ (terminal)
                              └────────────┘
```

**状态语义**：

| 状态 | 含义 | 进入条件 | 退出条件 |
|------|------|---------|---------|
| pending | 任务已建，未分配执行器 | create | scheduler 分配后转 scheduling |
| scheduling | 至少 1 个 subtask 已分配但全部未开始下载 | 任一 subtask 状态 ∈ {assigned} | 任一 subtask 进入 downloading |
| downloading | 至少 1 个 subtask 在下载或上传 | 任一 subtask ∈ {downloading, verifying_local, uploading, verifying_remote} | 全部 subtask 终态 |
| verifying | 全部 subtask 已 verified，等任务级最终校验 | 全部 subtask ∈ {verified} | 全部远端校验通过 → completed；失败 → failed |
| completed | 终态 | 全部远端校验通过 | — |
| failed | 终态（含部分失败） | 任一 subtask ∈ {failed_permanent} 且不可恢复 | — |
| cancelling | 用户已请求取消，等 in-flight 处置完成 | 用户调 `/cancel` | 全部 subtask 进入 {cancelled / completed}；保留 completed 的文件 |
| cancelled | 终态 | 所有 in-flight 处置完成 | — |

🔒 **不变量 5**：任务级最终校验必须遍历**所有** subtask 的 `expected_sha256 == actual_sha256`，不能只看 size（修复 v1.0 §5.1.5 仅 size 校验的漏洞）。

### 3.2 子任务状态机（FileSubTask.status）

```
       create
         │
         ▼
   ┌───────────┐                ┌─────────────────────┐
   │  pending  │◄───────────────│ paused_external     │
   └─────┬─────┘                │ (HF/S3 全局降级)    │
         │ CAS                  └─────────────────────┘
         ▼ (含 fence token)              ▲
   ┌───────────┐                         │
   │ assigned  │ ─── reclaim ──── pending│
   └─────┬─────┘ (executor 失联)         │
         │ executor 接受                 │
         ▼                               │
   ┌─────────────┐                       │
   │ downloading │ ─── 全局 429 ────────►│
   └─────┬───────┘                       │
         │ chunks 全部完成 + 流式 sha    │
         ▼                               │
   ┌─────────────┐                       │
   │ verifying_local │                   │
   └─────┬───────┘                       │
         │ 本地 sha 通过                 │
         ▼                               │
   ┌─────────────┐                       │
   │ uploading   │  (S3 multipart)       │
   └─────┬───────┘                       │
         │ multipart 完成                │
         ▼                               │
   ┌────────────────┐                    │
   │ verifying_remote│ (远端 SHA256+size)│
   └─────┬──────────┘                    │
         │ 通过                          │
         ▼                               │
   ┌─────────────┐                       │
   │  verified   │ (terminal-success)    │
   └─────────────┘                       │
                                         │
   失败路径：                            │
   {downloading, uploading} ── ENOSPC ──►│ paused_disk_full
   {*} ── 永久错误 (license/auth 401) ──►│ failed_permanent (terminal)
   cancelling ── ─────────────────────►│ cancelled (terminal)
```

**关键修订**（相对 v1.x）：

- ✅ 拆分 `transferring` / `uploading` 二选一为 **`uploading`**（统一命名，废弃 transferring）
- ✅ 新增 `verifying_local` / `verifying_remote` 两段校验，明确何时哪个 SHA256 被算
- ✅ 新增 `paused_external` 状态（HF/S3 全局降级时不进 failed，详见 03 §8）
- ✅ 新增 `paused_disk_full` 状态（ENOSPC 不当作可重试，详见 03 §3.7）

🔒 **不变量 6**：`assigned` → `downloading` 必须携带 `assignment_token`（详见 03 §2），否则 controller 拒绝。这是防双发的核心。

### 3.3 节点健康状态机（ExecutorProfile.status）

```
                ┌──────────┐
   注册成功     │          │   首次心跳
   ────────►   │ joining  │ ──────────►
                │          │
                └──────────┘
                                │
                                ▼
                ┌────────────────────────────────────┐
          ┌───► │            healthy                 │ ◄───┐
          │     │  (正常分配高优先级任务)             │     │
          │     └──────┬──────────────┬─────────────┘     │
          │            │              │                    │
          │     单次失败           连续心跳超时          probationary
          │            │              │ (3 次)             │ canary 通过
          │            ▼              ▼                    │
          │     ┌──────────┐   ┌──────────┐               │
          │     │ degraded │   │ suspect  │               │
          │     │  仅小任务 │   │ 不分配新任务│             │
          │     └────┬─────┘   └────┬──────┘               │
          │          │              │ 心跳恢复             │
          │   小任务连续 N=5         │  ───────────────────┘
          │   次成功                 │
          │          │              │ 确认故障 (timeout 60s)
          │          │              ▼
          │          │        ┌──────────┐
          │          │        │  faulty  │
          │          └────────│ 隔离+reclaim│
          │                   └────┬─────┘
          │                        │
          │           被动等待心跳重连
          │                        ▼
          │                  ┌──────────────┐
          │                  │ probationary │
          │                  │ canary 模式   │
          │                  └────┬─────────┘
          │                       │ canary 失败 N=2
          │                       │
          └───────────────────────┘ canary 通过 N=3
```

**关键修订**（相对 v1.4 §1.1）：

- ✅ 修复 D3 死循环：transition 时显式 reset `consecutive_failures = 0`，且 `degraded_failure_streak` 与 `suspect` 路径独立计数
- ✅ probing → probationary：单纯 HTTP 200 不算恢复，必须通过 1 个小 canary 任务
- ✅ Controller 不主动 probe（不变量 4），只被动接受心跳

🔒 **不变量 7**：每次状态 transition 必须显式记录 `(from, to, reason, ts)` 到 `executor_status_history` 表，便于追溯。

### 3.4 跃迁矩阵（机器可读）

```yaml
# tasks.yaml — 任务状态机（用于 CI 断言）
state_machine: download_task
states: [pending, scheduling, downloading, verifying, completed, failed, cancelling, cancelled]
terminal: [completed, failed, cancelled]
transitions:
  - {from: pending,     to: scheduling,  on: scheduler.assign_first_subtask}
  - {from: scheduling,  to: downloading, on: any_subtask.enter_downloading}
  - {from: scheduling,  to: cancelling,  on: user.cancel}
  - {from: downloading, to: verifying,   on: all_subtasks.verified}
  - {from: downloading, to: cancelling,  on: user.cancel}
  - {from: downloading, to: failed,      on: any_subtask.failed_permanent}
  - {from: verifying,   to: completed,   on: final_check.pass}
  - {from: verifying,   to: failed,      on: final_check.fail}
  - {from: cancelling,  to: cancelled,   on: all_inflight.handled}
illegal_transitions: # 必须 CI 断言为不可达
  - {from: completed, to: ANY}
  - {from: failed,    to: ANY}
  - {from: cancelled, to: ANY}
```

`subtasks.yaml` / `executors.yaml` 同样格式。CI 中 `pytest tests/test_state_machine.py` 必须断言所有不在 `transitions` 列表的跃迁都被实现拒绝。

---

## 4. 权威数据模型

> 仅在此处定义，其他文档只引用不复制。

### 4.1 租户层（v2.0 新增）

```sql
CREATE TABLE tenants (
    id                BIGSERIAL PRIMARY KEY,
    slug              VARCHAR(64) UNIQUE NOT NULL,    -- URL-safe，如 "team-a"
    display_name      VARCHAR(128) NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    quota_bytes_month BIGINT NOT NULL DEFAULT 0,      -- 0 = 无限制
    quota_concurrent  INT NOT NULL DEFAULT 10,
    quota_storage_gb  BIGINT NOT NULL DEFAULT 1024,
    is_active         BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE projects (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   BIGINT NOT NULL REFERENCES tenants(id),
    name        VARCHAR(128) NOT NULL,
    storage_id  BIGINT REFERENCES storage_backends(id),  -- 默认存储后端
    UNIQUE (tenant_id, name)
);

CREATE TABLE users (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     BIGINT NOT NULL REFERENCES tenants(id),
    oidc_subject  VARCHAR(256) UNIQUE NOT NULL,          -- OIDC 'sub'
    email         VARCHAR(256),
    role          VARCHAR(32) NOT NULL,                  -- admin / operator / viewer
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_users_tenant ON users(tenant_id);
```

🔒 **不变量 8**：所有业务表（tasks, subtasks, executors, storage_backends, audit_log...）必须包含 `tenant_id` 外键。CI 通过 information_schema 检查。

### 4.2 任务与子任务

```sql
CREATE TABLE download_tasks (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          BIGINT NOT NULL REFERENCES tenants(id),
    project_id         BIGINT NOT NULL REFERENCES projects(id),
    owner_user_id      BIGINT NOT NULL REFERENCES users(id),
    repo_id            VARCHAR(256) NOT NULL,            -- "deepseek-ai/DeepSeek-V3"
    revision           VARCHAR(64) NOT NULL,             -- 强制 40-char git sha，禁止 'main'
    storage_id         BIGINT NOT NULL REFERENCES storage_backends(id),
    path_template      VARCHAR(512) NOT NULL,            -- "{tenant}/{repo_id}/{revision}/{filename}"
    priority           SMALLINT NOT NULL DEFAULT 1,      -- 0=lowest, 3=highest
    status             VARCHAR(32) NOT NULL,
    is_simulation      BOOLEAN NOT NULL DEFAULT FALSE,
    download_bytes_limit BIGINT,                          -- 单任务字节配额（NULL = 不限）
    upgrade_from_revision VARCHAR(64),                    -- 增量下载：基线 revision
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at       TIMESTAMPTZ,
    cancelled_at       TIMESTAMPTZ,
    error_message      TEXT,
    trace_id           VARCHAR(32)                        -- OpenTelemetry trace id
);

CREATE INDEX idx_tasks_tenant_status ON download_tasks(tenant_id, status);
CREATE UNIQUE INDEX idx_tasks_dedup ON download_tasks(tenant_id, repo_id, revision)
    WHERE status NOT IN ('failed', 'cancelled');         -- 同租户同 revision 不重复

CREATE TABLE file_subtasks (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id             UUID NOT NULL REFERENCES download_tasks(id) ON DELETE CASCADE,
    tenant_id           BIGINT NOT NULL,                  -- 冗余便于查询
    filename            VARCHAR(512) NOT NULL,            -- 来自 HF siblings[].rfilename，已校验路径穿越
    file_size           BIGINT,                           -- 来自 HF；nullable（小文件）
    expected_sha256     VARCHAR(64),                      -- 来自 HF（LFS）；非 LFS 文件下载后填入
    status              VARCHAR(32) NOT NULL,
    executor_id         VARCHAR(64),                      -- 当前 owner（NULL 表示 pending）
    executor_epoch      BIGINT,                           -- 防止 stale executor 写入
    assignment_token    UUID,                             -- fence token，每次 assign 重新生成
    chunks_total        INT,
    chunks_completed    INT NOT NULL DEFAULT 0,
    bytes_downloaded    BIGINT NOT NULL DEFAULT 0,
    multipart_upload_id VARCHAR(256),                     -- S3 multipart，崩溃恢复用
    actual_sha256       VARCHAR(64),                      -- 实际计算结果
    retry_count         INT NOT NULL DEFAULT 0,
    last_error          TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at        TIMESTAMPTZ,

    -- 防双发的关键约束
    UNIQUE (task_id, filename)
);

CREATE INDEX idx_subtasks_status ON file_subtasks(status, executor_id);
CREATE INDEX idx_subtasks_pending ON file_subtasks(task_id) WHERE status = 'pending';
```

### 4.3 执行器

```sql
CREATE TABLE executors (
    id                 VARCHAR(64) PRIMARY KEY,           -- 形如 "host-12.local-worker-1"
    tenant_id          BIGINT REFERENCES tenants(id),     -- NULL = 系统级共享
    host_id            VARCHAR(64) NOT NULL,              -- 规范化主机 id（不是 hostname）
    parent_executor_id VARCHAR(64),                       -- 单机多执行器场景
    enrollment_token_id BIGINT NOT NULL,                  -- 注册时使用的 token
    cert_fingerprint   VARCHAR(128) NOT NULL,             -- mTLS 客户端证书指纹
    epoch              BIGINT NOT NULL,                   -- 单调递增；每次 register 自增
    status             VARCHAR(32) NOT NULL,              -- joining/healthy/degraded/suspect/faulty/probationary
    health_score       SMALLINT NOT NULL DEFAULT 100,     -- 0..100
    last_heartbeat_at  TIMESTAMPTZ,
    consecutive_failures INT NOT NULL DEFAULT 0,
    degraded_failure_streak INT NOT NULL DEFAULT 0,       -- 与 suspect 路径独立
    capabilities       JSONB NOT NULL DEFAULT '{}',       -- {storage_backends: [...], regions: [...]}
    nic_speed_gbps     SMALLINT,
    disk_free_gb       BIGINT,
    disk_total_gb      BIGINT,
    parts_dir_bytes    BIGINT NOT NULL DEFAULT 0,         -- 临时区占用
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    deactivated_at     TIMESTAMPTZ
);

CREATE INDEX idx_executors_status ON executors(status, last_heartbeat_at);

CREATE TABLE executor_status_history (
    id            BIGSERIAL PRIMARY KEY,
    executor_id   VARCHAR(64) NOT NULL,
    from_status   VARCHAR(32) NOT NULL,
    to_status     VARCHAR(32) NOT NULL,
    reason        VARCHAR(64) NOT NULL,                   -- 'heartbeat_timeout' / 'task_fail_streak' / ...
    occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

🔒 **不变量 9**：`epoch` 单调递增；`(executor_id, epoch)` 是任意操作的因果时钟。所有从 executor 上来的请求必须带当前 epoch。详见 03 §2。

### 4.4 存储与凭证

```sql
CREATE TABLE storage_backends (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     BIGINT REFERENCES tenants(id),         -- NULL = 系统共享
    name          VARCHAR(128) NOT NULL,
    backend_type  VARCHAR(32) NOT NULL,                  -- s3 / obs / minio / nfs / local
    region        VARCHAR(64),
    config_encrypted BYTEA NOT NULL,                     -- envelope encryption (KMS-DEK)
    is_default    BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (tenant_id, name)
);

-- 长期凭证从不离开此表；下发到 executor 的是 STS 临时凭证
```

### 4.5 配额与计量（v2.0 新增）

```sql
CREATE TABLE usage_records (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     BIGINT NOT NULL,
    project_id    BIGINT,
    user_id       BIGINT,
    task_id       UUID,
    metric        VARCHAR(64) NOT NULL,                  -- bytes_downloaded / bytes_egress / storage_gb_hour
    value         BIGINT NOT NULL,
    region_pair   VARCHAR(64),                           -- 'us-east-1->cn-north-1' 用于成本归属
    occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_usage_tenant_metric ON usage_records(tenant_id, metric, occurred_at);

CREATE TABLE quota_snapshots (
    tenant_id          BIGINT PRIMARY KEY REFERENCES tenants(id),
    bytes_used_month   BIGINT NOT NULL DEFAULT 0,
    storage_gb_used    BIGINT NOT NULL DEFAULT 0,
    concurrent_tasks   INT NOT NULL DEFAULT 0,
    last_recomputed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 4.6 字段引入版本表（开发参考）

| 字段 | 引入版本 | 备注 |
|------|---------|------|
| `download_tasks.tenant_id` | v2.0 | 多租户必需 |
| `download_tasks.path_template` | v2.0 | 取代 v1.0 `target_dir` |
| `download_tasks.upgrade_from_revision` | v2.0 | 增量下载（详见 06 §2） |
| `file_subtasks.executor_epoch` | v2.0 | 防 stale write |
| `file_subtasks.assignment_token` | v2.0 | Fence token（详见 03 §2） |
| `file_subtasks.multipart_upload_id` | v2.0 | 崩溃恢复 |
| `file_subtasks.actual_sha256` | v2.0 | 与 expected 对比 |
| `executors.epoch` | v2.0 | 单调递增 |
| `executors.host_id` | v2.0 | 规范化（解决 hostname 不唯一） |
| `executors.parent_executor_id` | v2.0 | 单机多执行器 |
| `usage_records` 整表 | v2.0 | 配额与计量 |

废弃字段（v1.x 中存在但 v2.0 删除）：

- `download_tasks.target_dir` —— 由 `storage_id + path_template` 取代
- `file_subtasks.local_path` —— 改为 executor 本地 .parts/ 目录的运行时变量，不持久化到 DB
- `file_subtasks.transferring` 状态 —— 与 `uploading` 二选一，统一为 `uploading`

---

## 5. 模块职责边界

### 5.1 Controller（中控）

**职责**：

1. 任务调度（CAS-then-enqueue + epoch + assignment token）
2. 状态汇总与持久化
3. HF API 唯一入口（reverse-proxy）
4. 凭证管理（HF Token / S3 STS）
5. 健康监控（被动接受心跳，不主动 probe）
6. 配额检查与用量上报
7. WebSocket fan-out（snapshot+delta+seq）

**不做**：

- 不直接下载 / 上传任何文件（执行器做）
- 不做 UI 渲染逻辑
- 不持有用户敏感信息明文（envelope encryption）

### 5.2 Executor（执行器）

**职责**：

1. 多线程下载（DirectOffsetDownloader）
2. 本地校验（流式 SHA256 单线程；多线程方案见 03 §6）
3. 上传到 storage backend（S3 multipart，upload_id 持久化）
4. 心跳上报 + 进度上报
5. .parts/ 临时区管理（持久卷，启动 GC）

**不做**：

- 不直连 HF（必须经 Controller proxy）
- 不持长期 storage 凭证（只持 STS 临时）
- 不做调度决策

### 5.3 单机多执行器（v2.0 强化）

**模型**：一台主机可启动 N 个 executor 进程，共享 NIC，通过 `parent_executor_id` 关联。

**调度约束**：

🔒 **不变量 10**：调度器在分配 subtask 时，不允许把同一文件的多个 chunk 分给同一 `host_id` 下的不同 executor（NIC 是共享瓶颈）。这是 `MultiExecutorAwareScheduler` 的核心约束。

**带宽分配**：

- 每个 host 上报 `nic_speed_gbps`，host 下所有 executor 的总下载带宽不超过 80% NIC 容量（避免饥饿其他业务）
- 单文件并发线程数 = `min(8, host_nic_gbps * 8 / num_active_executors_on_host)`

### 5.4 UI

**职责**：

1. 用户登录（OIDC）+ 任务管理界面
2. 实时进度展示（订阅 WSS，渲染 snapshot+delta）
3. 模型搜索（调 Controller 的 `/api/models/search`，永不直连 HF）

**不做**：

- 不做调度决策（如旧 v1.4 §6.4 的"GLM-4 完成后 node-3/4/6 将自动分配给 Qwen2.5"——这是预测，必须由 Controller 提供 `/api/scheduler/forecast` 接口；UI 只渲染）
- 不直接调 HF API
- 不渲染来自 executor 的不可信字符串（v-html 禁用，详见 04 §4）

---

## 6. 技术选型

### 6.1 强制选型

| 组件 | 选型 | 原因 / 不变量 |
|------|------|-------------|
| **Controller DB** | **PostgreSQL ≥ 14** | 需要 row-level CAS、唯一约束、`gen_random_uuid()`、`pgaudit`。SQLite 仅用于单机测试（明确禁止生产用） |
| **Controller 进程模型** | **active/standby 双副本** | RTO=10min（不变量来自 05 §6） |
| **Executor → Controller 通道** | **HTTPS + mTLS + JWT** | 详见 04 §2 |
| **Storage 抽象** | **`StorageBackend` 接口**，子类 S3/OBS/MinIO/NFS/Local | 解耦；NFS 仅供单机测试 |
| **ID 生成** | UUIDv7（任务/子任务）、自增 BIGINT（用户/审计） | UUIDv7 时间排序；BIGINT 节省存储 |
| **观测** | Prometheus + Loki + OpenTelemetry → Tempo/Jaeger | 详见 05 §1 |
| **任务队列** | 不引入 Celery/RQ；用 PG `SKIP LOCKED` | 减少依赖；详见 03 §4 |

### 6.2 软选型（可替换）

| 组件 | 默认 | 可替换为 |
|------|------|---------|
| WebSocket 库 | FastAPI native | 抽象 `ProgressTransport` 接口，支持 SSE / long-poll 兜底 |
| 哈希 | SHA256（HF 给的） + BLAKE3（多线程加速） | 仅当 HF 全面切到 BLAKE3 时切换 |
| 限流 | `slowapi` | Redis token bucket（多 controller 协调） |
| KMS | AWS KMS / 华为云 KMS | HashiCorp Vault Transit |

### 6.3 明确放弃的方案

📝 **决策**：

- **不用 Celery / RQ**：增加依赖、增加运维面、PG `SKIP LOCKED` + 自研 worker pool 已够用
- **不用 Redis 做主存**：状态必须强一致 + 持久；Redis 仅用作可选的限流协调层
- **不用 etcd / Zookeeper 做 leader 选举**：PostgreSQL `pg_advisory_lock` + WAL replication 已能实现 active/standby
- **不在 v2.0 实现 active-active**：复杂度过高，v2.x roadmap，详见 06 §9
- **不内置 K8s 部署**：提供 docker-compose + Helm chart，但不绑死

---

## 7. 关键不变量索引

为方便 CI 断言和 review，所有不变量集中索引：

| ID | 内容 | 验证方式 |
|----|------|---------|
| 1 | UI 永不直连 HF API | 网络策略 + UI 静态扫描 |
| 2 | HF Token 不离开 Controller | 代码 lint：禁 `Authorization: Bearer hf_` 出现在 executor 代码 |
| 3 | Executor 不持长期 storage 凭证 | 配置扫描 + 运行时审计 |
| 4 | Controller 不主动反向连接 Executor | 网络策略 + 代码扫描 |
| 5 | 任务级最终校验必须比对 `expected_sha256 == actual_sha256` | 单测 |
| 6 | `assigned → downloading` 必须携带 `assignment_token` | API 测试 |
| 7 | Executor 状态 transition 写 `executor_status_history` | DB trigger / 单测 |
| 8 | 业务表必须有 `tenant_id` | information_schema 扫描 |
| 9 | `(executor_id, epoch)` 是因果时钟 | API 测试 |
| 10 | 同一文件的多 chunk 不分给同 host_id 下不同 executor | 调度器单测 |
| 11 | HF 永远是 SHA256 真值来源（详见 06 §1.2） | 多源测试 |
| 12 | 跨源下载完成后必须比对 HF sha256；不一致则源黑名单 24h | E2E 多源 fault injection |
| 13 | HF 不可用时默认拒绝下载（除非用户 explicit `trust_non_hf_sha256`） | 多源故障注入测试 |
| 14 | `(tenant_id, repo_id, revision, filename, sha256)` 在存储中只存一份 | DB UNIQUE 约束 + GC 单测 |
| 15 | AI Copilot 不能超越调用用户的 RBAC 权限（详见 12 §6.2） | AI 注入/越权测试 AI-SEC-* |
| 16 | 所有 AI 触发的写操作必须写 audit_log，含 `actor_kind=ai_copilot` | AI 工具单测 + audit chain 校验 |
| 17 | AI 写操作必须用户 confirm；read-only 工具可免确认 | 协议测试 U-AI-T-005 |
| 18 | LLM token 配额与下载流量配额隔离 | quota 单测 I-AI-Q-001 |
| 19 | 网络查询工具的输出必须 sanitize 后才进 LLM context（含来源标记 + 注入检测） | 安全测试 U-AI-S-001..010 |

---

## 8. 与其他文档的链接

- **API / 协议**：→ [02-protocol.md](./02-protocol.md)
- **Fence token / 恢复语义**：→ [03-distributed-correctness.md](./03-distributed-correctness.md)
- **认证 / 多租户 / 配额 / 合规**：→ [04-security-and-tenancy.md](./04-security-and-tenancy.md)
- **SLO / Runbook / 备份**：→ [05-operations.md](./05-operations.md)
- **多源 / 增量 / CLI / 集成**：→ [06-platform-and-ecosystem.md](./06-platform-and-ecosystem.md)
