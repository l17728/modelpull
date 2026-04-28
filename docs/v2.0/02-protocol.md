# 02 — API 与协议

> 角色：实现 SDK / 集成方只读这份。
> 取代：v1.0 §13（整章作废），v1.4 §5（实时进度协议），v1.5 中 §1 内零散的 API 修订。

---

## 0. 从历史文档迁移指引

| 旧位置 | v2.0 位置 |
|--------|----------|
| `design_document.md` §13 API 接口定义 | 本文 §2 OpenAPI（整章作废，仅作历史） |
| `design_document.md` §13.5 心跳 | 本文 §4 |
| `design_document_fault_tolerance_and_visualization.md` §5 实时进度协议 | 本文 §5 |
| `design_document_fault_tolerance_and_visualization.md` §1.6 心跳协议 | 本文 §4 |
| `design_document_review_and_e2e.md` §1.6 任务下发 CAS | 本文 §6 |

---

## 1. 协议总览

| 通道 | 协议 | 鉴权 | 用途 |
|------|------|------|------|
| UI ↔ Controller REST | HTTPS 1.3 | OIDC + Bearer JWT | 任务管理、模型搜索、设置 |
| UI ↔ Controller WS | WSS（子协议握手） | JWT 子协议 + Origin 白名单 | 任务进度推送 |
| Executor ↔ Controller | HTTPS 1.3 + mTLS | mTLS + Executor JWT + HMAC body | 心跳、任务领取、上报 |
| Executor → HF（via Controller） | HTTPS 1.3 | Controller 注入 HF Token | 下载（reverse-proxy） |
| Executor → Storage | HTTPS（S3 protocol） | STS 临时凭证（TTL=1h） | 上传 |

🔒 **协议不变量**：

- TLS ≥ 1.3，禁用 1.2 以下
- 所有响应必须 `Content-Type: application/json; charset=utf-8`
- 错误响应统一格式（详见 §7）
- 所有时间字段 ISO 8601 + UTC（`2026-04-28T10:30:00Z`）
- 所有字节字段单位为字节（不缩写为 KB/MB/GB），命名后缀 `_bytes`

---

## 2. OpenAPI 3.1 完整 schema（权威来源）

> 实际维护：`api/openapi.yaml`（在仓库根）。本文档展示核心结构，CI 检查与代码同步。

### 2.1 schema 文件结构

```
api/
├── openapi.yaml                    # 主入口，引用以下分片
├── schemas/
│   ├── tenant.yaml
│   ├── task.yaml                   # DownloadTask + FileSubTask + Chunk
│   ├── executor.yaml
│   ├── source.yaml                 # 多源相关，详见 06 §1
│   ├── storage.yaml
│   ├── error.yaml
│   └── audit.yaml
├── paths/
│   ├── tasks.yaml
│   ├── executors.yaml
│   ├── models.yaml
│   ├── sources.yaml
│   └── ...
└── examples/
    └── ...
```

### 2.2 主要数据 schema 摘要

```yaml
DownloadTask:
  type: object
  required: [id, tenant_id, repo_id, revision, storage_id, status, created_at]
  properties:
    id: {type: string, format: uuid}
    tenant_id: {type: integer, format: int64}
    project_id: {type: integer, format: int64}
    owner_user_id: {type: integer, format: int64}
    repo_id: {type: string, pattern: '^[A-Za-z0-9_\-]{1,96}/[A-Za-z0-9_.\-]{1,96}$'}
    revision:
      type: string
      pattern: '^[0-9a-f]{40}$'    # 强制 40-char git sha；禁 'main'/'master'
    storage_id: {type: integer}
    path_template: {type: string, maxLength: 512}
    priority: {type: integer, minimum: 0, maximum: 3}
    status:
      type: string
      enum: [pending, scheduling, downloading, verifying, completed, failed, cancelling, cancelled]
    source_strategy:
      type: string
      enum: [auto_balance, pin_huggingface, pin_modelscope, fastest_only]
    source_blacklist:
      type: array
      items: {type: string}
    is_simulation: {type: boolean}
    download_bytes_limit: {type: integer, format: int64, nullable: true}
    upgrade_from_revision: {type: string, nullable: true}
    progress:
      type: object
      properties:
        files_total: {type: integer}
        files_completed: {type: integer}
        bytes_total: {type: integer, format: int64}
        bytes_downloaded: {type: integer, format: int64}
        eta_seconds: {type: integer, nullable: true}
    created_at: {type: string, format: date-time}
    completed_at: {type: string, format: date-time, nullable: true}
    error_message: {type: string, nullable: true}
    trace_id: {type: string, maxLength: 32}

FileSubTask:
  type: object
  required: [id, task_id, filename, status]
  properties:
    id: {type: string, format: uuid}
    task_id: {type: string, format: uuid}
    filename: {type: string, maxLength: 512}
    file_size: {type: integer, format: int64, nullable: true}
    expected_sha256: {type: string, nullable: true, pattern: '^[0-9a-f]{64}$'}
    actual_sha256: {type: string, nullable: true}
    status:
      type: string
      enum: [pending, assigned, downloading, verifying_local, uploading,
             verifying_remote, verified, failed_permanent, paused_external,
             paused_disk_full, cancelling, cancelled]
    executor_id: {type: string, nullable: true}
    executor_epoch: {type: integer, format: int64, nullable: true}
    assignment_token: {type: string, format: uuid, nullable: true}
    source_id: {type: string, nullable: true}    # 实际下载使用的源
    chunks_total: {type: integer}
    chunks_completed: {type: integer}
    bytes_downloaded: {type: integer, format: int64}
    multipart_upload_id: {type: string, nullable: true}
    retry_count: {type: integer}
    last_error: {type: string, nullable: true}

ErrorResponse:
  type: object
  required: [code, message, trace_id]
  properties:
    code: {type: string}             # MACHINE_READABLE_CODE
    message: {type: string}          # human-readable
    trace_id: {type: string}
    details:
      type: object                   # 错误特定字段
      additionalProperties: true
```

### 2.3 公共 header

| Header | 含义 | 必需 |
|--------|------|------|
| `Authorization: Bearer <jwt>` | 用户/Executor JWT | 全部 |
| `X-Tenant-Slug: <slug>` | 显式指定租户（admin 切换用） | 仅 admin |
| `X-Request-Id: <uuid>` | 客户端生成，便于日志追溯 | 推荐 |
| `X-Idempotency-Key: <key>` | 写操作幂等 | POST 推荐 |
| `Traceparent: <w3c>` | OpenTelemetry trace propagation | 自动 |

---

## 3. 任务 API

### 3.1 创建任务

```http
POST /api/tasks
Authorization: Bearer <user_jwt>
X-Idempotency-Key: <client_uuid>
Content-Type: application/json

{
  "repo_id": "deepseek-ai/DeepSeek-V3",
  "revision": "abc123def456...",                    // 必须 40-char sha；'main' 拒绝
  "storage_id": 5,                                   // 不传则用 project 默认
  "path_template": "{tenant}/{repo_id}/{revision}",
  "priority": 2,                                     // 0..3
  "source_strategy": "auto_balance",                 // 详见 06 §1
  "source_blacklist": [],
  "file_filter": "core_only",                        // core_only / all / glob
  "file_glob": null,                                 // file_filter=glob 时填
  "is_simulation": false,
  "upgrade_from_revision": null,
  "download_bytes_limit": null,
  "trust_non_hf_sha256": false                       // 见 06 §1.13 已知风险
}

201 Created
{
  "id": "uuid-...",
  "status": "pending",
  "trace_id": "...",
  "files_to_download": 163,
  "estimated_bytes": 740088332288,
  "speed_probe_eta_seconds": 8                       // 测速预计耗时（详见 06 §1.8）
}
```

**错误码**：

| code | HTTP | 含义 |
|------|------|------|
| `INVALID_REVISION` | 400 | revision 不是 40-char sha |
| `REPO_NOT_FOUND` | 404 | HF 上找不到（其他源也找不到） |
| `REPO_GATED` | 403 | gated 模型且 user 无授权 |
| `QUOTA_EXCEEDED` | 429 | 租户字节/任务数配额耗尽 |
| `STORAGE_BACKEND_NOT_FOUND` | 404 | storage_id 无效 |
| `DUPLICATE_TASK` | 409 | 同租户已有相同 (repo_id, revision) 进行中任务 |
| `IDEMPOTENCY_REPLAY` | 200 | 同 idempotency key 已处理（返回原始结果） |

### 3.2 查询任务

```http
GET /api/tasks?status=downloading&limit=50&cursor=...
Authorization: Bearer <user_jwt>

200 OK
{
  "items": [DownloadTask, ...],
  "next_cursor": "...",
  "total_estimated": 127
}
```

```http
GET /api/tasks/{id}
GET /api/tasks/{id}/subtasks
GET /api/tasks/{id}/source-allocation     # 见 06 §1.11，多源分配视图数据
GET /api/tasks/{id}/events                # 任务事件日志
```

### 3.3 取消任务

```http
POST /api/tasks/{id}/cancel
{ "reason": "user_request" }                       // optional

202 Accepted
{
  "status": "cancelling",
  "in_flight_subtasks": 8                          // 仍在进行的，会逐个 transition
}
```

🔒 **不变量**：取消是异步的；任务进入 `cancelling`，所有 in-flight subtask 完成后变 `cancelled`。已 `verified` 的 subtask 保留文件（用户重启任务可复用）。详见 03 §7。

### 3.4 重试失败子任务

```http
POST /api/tasks/{id}/retry?subtask_ids=uuid1,uuid2
204 No Content
```

### 3.5 调整优先级

```http
PATCH /api/tasks/{id}
{ "priority": 3 }
```

### 3.6 增量更新（升级到新 revision）

```http
POST /api/tasks/{id}/upgrade
{
  "to_revision": "new-sha-...",
  "keep_old_files_for_days": 7
}
```

详见 06 §2。

### 3.7 模型搜索

```http
GET /api/models/search?query=deepseek&limit=20
GET /api/models/{repo_id}/info?revision=abc123      # 文件清单 + sha256 + 多源覆盖

200 OK
{
  "repo_id": "deepseek-ai/DeepSeek-V3",
  "revision": "abc123",
  "files": [
    {
      "filename": "model-00001-of-00163.safetensors",
      "size": 4322000000,
      "sha256": "...",
      "available_on": ["huggingface", "modelscope", "hf_mirror"]
    },
    ...
  ],
  "license": "apache-2.0",
  "is_gated": false
}
```

🔒 controller 是 HF API 唯一出口（不变量 1）；UI 不直连 HF。

---

## 4. Executor 心跳协议

> 取代 v1.0 §13.5 / v1.4 §1.6 / v1.5 §1.4

### 4.1 协议要求

每个心跳包：

- HTTPS POST，mTLS 双向认证
- `Authorization: Bearer <executor_jwt>`（详见 04 §2.2）
- `X-Heartbeat-Nonce: <128-bit-random>`（防重放，5min 滑动窗去重）
- `X-Heartbeat-Timestamp: <unix-ms>`（与 server 时钟差 ≤ 60s）
- `X-Heartbeat-HMAC: <hex-sha256>` ← `HMAC(enrollment_secret, body || nonce || timestamp)`

### 4.2 Request body

```http
POST /api/executors/heartbeat
Content-Type: application/json

{
  "executor_id": "host-12.local-worker-1",
  "epoch": 14,                              // 单调递增；register 时分配
  "host_id": "host-12.local",
  "parent_executor_id": null,
  "timestamp": "2026-04-28T10:30:00.000Z",
  "uptime_seconds": 3600,

  "system": {
    "nic_speed_gbps": 10,
    "nic_utilization_percent": 67,
    "disk_total_gb": 2000,
    "disk_free_gb": 1230,
    "parts_dir_bytes": 12345678901,
    "memory_used_percent": 34,
    "load_avg_1min": 4.2,
    "active_threads": 16,
    "running_subtasks": 5,
    "multi_executor": {                     // 当 host 上有多 executor 时
      "siblings_on_host": ["host-12.local-worker-2"],
      "host_total_running": 7
    }
  },

  "source_health": {                        // executor 视角的源可达性
    "huggingface": {"reachable": true, "rtt_ms": 320, "last_5xx_ago_s": 1200},
    "modelscope":  {"reachable": true, "rtt_ms":  18, "last_5xx_ago_s": null},
    "hf_mirror":   {"reachable": true, "rtt_ms":  35, "last_5xx_ago_s": null}
  },

  "tasks": [                                 // 进行中的 subtask 进度
    {
      "subtask_id": "uuid-...",
      "task_id": "uuid-...",
      "executor_epoch": 14,                  // 必须等于 outer epoch
      "assignment_token": "uuid-...",        // fence token，详见 03 §2
      "status": "downloading",
      "bytes_downloaded": 9876543210,
      "bytes_total": 17000000000,
      "chunks_total": 8,
      "chunks_completed": 5,
      "active_threads": 4,
      "current_speed_bytes_per_sec": 950000000,
      "speed_window_seconds": 5,             // 上面速度的统计窗口
      "eta_seconds": 75,
      "source_id": "modelscope",
      "remote_etag_seen": "abc..."
    }
  ],

  "completed_subtasks": [
    {
      "subtask_id": "uuid-...",
      "executor_epoch": 14,
      "assignment_token": "uuid-...",
      "actual_sha256": "...",
      "actual_size": 17000000000,
      "remote_storage_uri": "s3://bucket/...",
      "remote_etag": "...",
      "duration_seconds": 1820
    }
  ],

  "failed_subtasks": [
    {
      "subtask_id": "uuid-...",
      "error_code": "CHECKSUM_MISMATCH",
      "error_message": "expected ... got ...",
      "retryable": false,
      "attempted_sources": ["modelscope", "hf_mirror"]
    }
  ]
}
```

### 4.3 Response body

```json
{
  "server_timestamp": "2026-04-28T10:30:00.150Z",
  "epoch_check": "ok",
  "next_heartbeat_in_seconds": 10,
  "config_version": 42,
  "policies_to_apply": {
    "max_concurrent_per_executor": 5,
    "global_speed_limit_bytes_per_sec": null,
    "source_blacklist_global": []
  },

  "new_assignments": [
    {
      "subtask_id": "uuid-...",
      "task_id": "uuid-...",
      "filename": "model-00001-of-00163.safetensors",
      "expected_size": 4322000000,
      "expected_sha256": "...",
      "source_id": "modelscope",                    // 调度器选定的源
      "alternate_sources": ["hf_mirror", "huggingface"],
      "executor_epoch": 14,                         // echo
      "assignment_token": "fresh-uuid",             // 必须在 download 请求中回带
      "deadline_at": "2026-04-28T11:00:00Z",        // 超时未完成视为 stale
      "chunk_plan": [                                // 仅 chunk-level routing 时填
        {"chunk_index": 0, "byte_start": 0, "byte_end": 1073741823, "source_id": "modelscope"},
        {"chunk_index": 1, "byte_start": 1073741824, "byte_end": 2147483647, "source_id": "hf_mirror"}
      ],
      "storage_target": {
        "backend": "s3",
        "endpoint": "...",
        "bucket": "...",
        "key_prefix": "...",
        "sts_credentials": {                         // STS 临时凭证（TTL=1h）
          "access_key_id": "...",
          "secret_access_key": "...",
          "session_token": "...",
          "expires_at": "2026-04-28T11:30:00Z"
        }
      }
    }
  ],

  "cancellations": [                                  // 控制器要求取消的 subtask
    {"subtask_id": "uuid-...", "reason": "user_cancelled"}
  ],

  "reassignments": [                                  // 局部重平衡（详见 06 §1.8 阶段 B）
    {
      "subtask_id": "uuid-...",
      "chunks_to_release": [3, 4, 5]                  // 这些 chunk 释放给其他 executor
    }
  ],

  "speed_probe_request": {                            // 控制器请求测速（详见 06 §1.8）
    "task_id": "uuid-...",
    "sources": ["modelscope", "hf_mirror", "huggingface"],
    "probe_size_mb": 32,
    "deadline_at": "2026-04-28T10:30:08Z"
  }
}
```

### 4.4 心跳合并优化

为了控制 PG 写入量（OPS-09 容量瓶颈），单 controller 合并心跳：

- 每 200ms 批量 flush 一次心跳进度到 PG
- 单 executor 心跳进度 in-memory 即时更新，但 commit 走批
- 完成上报（completed_subtasks）必须立即写（强一致），不走批

🔒 **不变量**：`bytes_downloaded` 单调递增；如果 PG 中已存的值大于本次上报，以大者为准（防 retry 倒退）。

### 4.5 错误场景

| 服务端响应 | 含义 | Executor 行为 |
|----------|------|--------------|
| `200` | 正常 | 按 response 处理 |
| `401 EPOCH_MISMATCH` | 你的 epoch 落后（被替换） | 立即停止所有 subtask，重新 register |
| `401 CERT_REVOKED` | mTLS 证书被吊销 | 进入 `disabled` 状态，告警，不再尝试 |
| `409 CLOCK_SKEW` | 时间戳偏差 > 60s | 校时，下次再试 |
| `409 NONCE_REPLAY` | nonce 已用过 | 重新生成 nonce 重试 |
| `429 BACKOFF` | 服务端要求降速 | 按响应中 `retry_after_s` 等待 |
| `503 CONTROLLER_DEGRADED` | 控制器过载 | 心跳改为 30s，subtask 继续 |

---

## 5. WebSocket 进度推送

> 取代 v1.0 §13.4 / v1.4 §5

### 5.1 连接建立

```
WSS /ws/v1?token=<user_jwt>&filter=tasks:uuid1,uuid2,uuid3
Sec-WebSocket-Protocol: bearer.<jwt>
Origin: https://ui.dlw.example.com    # 必须在白名单
```

握手时校验：

1. JWT 有效 + scope 含 `progress.read`
2. Origin ∈ 白名单
3. 用户对所有 subscribe 的 task 有读权限（按 tenant + RBAC）

### 5.2 消息序列：snapshot + delta + seq

```jsonc
// 服务端首条：当前快照
{
  "type": "snapshot",
  "seq": 1,
  "ts": "2026-04-28T10:30:00Z",
  "tasks": [
    {"id": "uuid-1", "status": "downloading", "progress": {...}},
    ...
  ]
}

// 后续：增量
{
  "type": "delta",
  "seq": 2,
  "ts": "2026-04-28T10:30:00.500Z",
  "patches": [
    {
      "task_id": "uuid-1",
      "fields": {"bytes_downloaded": 12345678901, "eta_seconds": 720},
      "subtasks": [
        {"id": "uuid-s1", "fields": {"bytes_downloaded": 1000000000}}
      ]
    }
  ]
}

// 心跳保持
{"type": "ping", "seq": 0}    // server → client，每 20s
{"type": "pong"}              // client → server
```

🔒 **不变量**：`seq` 单调递增。client 检测到 gap → 主动发 `{"type": "resync"}`，server 回 snapshot。

### 5.3 推送限频

- 单 client 最多 2 条/秒
- 进度推送批量合并（100ms 窗口内的 patches 合并发送）
- 完成/失败事件不限频（关键事件即时）

### 5.4 客户端重连

```jsonc
// 重连时带最后看到的 seq
WSS /ws/v1?token=...&filter=...&last_seq=42

// 服务端响应：
// 1. 如果 last_seq 在 buffer 内（<1min 内）：从 last_seq+1 续推
// 2. 如果不在：发 snapshot 重新对齐
```

---

## 6. 任务下发：CAS-then-enqueue 协议

> 解决 D1 双发漏洞。详见 03 §2。

### 6.1 协议时序

```
Controller                              Executor
   │                                       │
   │   ◄─────  heartbeat ──────────────────│
   │         (含 active subtasks)         │
   │                                       │
   │  [SCHEDULER_LOCK]                     │
   │  CAS: UPDATE subtasks                 │
   │       SET status='assigned',          │
   │           executor_id=A,              │
   │           executor_epoch=14,          │
   │           assignment_token=NEW_TOKEN  │
   │       WHERE id=S AND status='pending' │
   │  IF rowcount == 0: skip               │
   │  IF rowcount == 1:                    │
   │      enqueue_for_response(S, TOKEN)   │
   │  [/SCHEDULER_LOCK]                    │
   │                                       │
   │  ─── HB response with assignment ────►│
   │      {subtask_id: S, token: NEW_TOKEN,│
   │       executor_epoch: 14, ...}        │
   │                                       │
   │                                       │ Executor 启动下载
   │                                       │ 用 (S, TOKEN, epoch=14) 标记本次任务
   │                                       │
   │   ◄─── download_request via proxy ────│
   │      X-Subtask-Id: S                  │
   │      X-Assignment-Token: TOKEN        │
   │      X-Executor-Epoch: 14             │
   │                                       │
   │  Verify: subtasks WHERE id=S AND      │
   │          assignment_token=TOKEN AND   │
   │          executor_epoch=14            │
   │  IF mismatch: 409 STALE_ASSIGNMENT    │
   │  IF match: proxy 请求到 HF/source     │
   │                                       │
```

🔒 **不变量**：assignment_token 一旦生成，整个 download → upload → complete 链路都要原样回带，controller 在每次写入时校验。

### 6.2 完成上报

```http
POST /api/executors/{eid}/subtasks/{sid}/complete
Authorization: Bearer <executor_jwt>
X-Assignment-Token: <token>
X-Executor-Epoch: 14

{
  "actual_sha256": "...",
  "actual_size": 17000000000,
  "remote_storage_uri": "s3://...",
  "remote_etag": "...",
  "remote_sha256_via_checksum": "...",       // S3 ChecksumSHA256
  "duration_seconds": 1820,
  "sources_used": ["modelscope"],
  "bytes_per_source": {"modelscope": 17000000000}
}

200 OK
{ "subtask_status": "verified" }

// 错误场景：
409 STALE_ASSIGNMENT  // (executor_id, epoch, token) 不匹配当前 owner
409 ALREADY_COMPLETED // 已被其他报告完成（双完成）
410 TOKEN_EXPIRED     // assignment_token 已被覆盖（用户取消后重启）
```

---

## 7. 错误格式与错误码

### 7.1 标准错误响应

```json
{
  "code": "MACHINE_READABLE_CODE",
  "message": "Human-readable description",
  "trace_id": "abc123def456",
  "details": {
    "field": "value"
  }
}
```

### 7.2 公共错误码

| code | HTTP | 含义 |
|------|------|------|
| `UNAUTHENTICATED` | 401 | JWT 缺失/失效 |
| `FORBIDDEN` | 403 | 鉴权不通过 |
| `NOT_FOUND` | 404 | 资源不存在 |
| `CONFLICT` | 409 | 状态冲突（含 stale assignment） |
| `RATE_LIMITED` | 429 | 触发限流，含 `retry_after_s` |
| `QUOTA_EXCEEDED` | 429 | 配额耗尽 |
| `INTERNAL` | 500 | 服务端异常 |
| `UPSTREAM_DEGRADED` | 503 | HF/源不可用 |

特定错误码见各 endpoint 描述。

---

## 8. 限流与配额

### 8.1 路由级 token bucket（slowapi）

| Endpoint | 默认限流 |
|---------|---------|
| `POST /api/tasks` | 10/h/user |
| `POST /api/executors/register` | 5/min/IP |
| `GET /api/models/search` | 30/min/user |
| `POST /api/executors/heartbeat` | 不限（mTLS 已限制来源） |
| WSS `/ws/v1` | 1 connection/user, 重连 cooldown 5s |

### 8.2 租户配额（强一致检查）

任务创建时（事务内）：

```sql
SELECT bytes_used_month, concurrent_tasks
FROM quota_snapshots
WHERE tenant_id = $1 FOR UPDATE;

IF bytes_used_month + estimated_bytes > tenants.quota_bytes_month: 429 QUOTA_EXCEEDED
IF concurrent_tasks >= tenants.quota_concurrent: 429 QUOTA_EXCEEDED
ELSE: INSERT INTO download_tasks ...
      UPDATE quota_snapshots SET concurrent_tasks=concurrent_tasks+1
```

详见 04 §7 配额章节。

---

## 9. 兼容性策略

- v2.0 是大版本变更：旧 v1.x SDK 不兼容
- 提供 `/api/v1/...` 兼容垫片层（仅读取，不支持新功能），生命周期 6 个月
- 新功能（多源、增量、配额）只在 v2 路径暴露

API 版本号通过 URL 前缀：`/api/v1/...` / `/api/v2/...`

---

## 10. 与其他文档的链接

- 数据模型：→ [01-architecture.md](./01-architecture.md) §4
- Fence token / 恢复语义：→ [03-distributed-correctness.md](./03-distributed-correctness.md)
- 认证 / 多租户 / 配额：→ [04-security-and-tenancy.md](./04-security-and-tenancy.md)
- SLI / 限流监控：→ [05-operations.md](./05-operations.md)
- 多源 source_strategy 详细：→ [06-platform-and-ecosystem.md](./06-platform-and-ecosystem.md)
