# 05 — 运维与可观测性

> 角色：SRE / on-call 工作手册。
> 取代：v1.5 §4 日志与可观测性，散落在各文的运维提示。

---

## 0. 从历史文档迁移指引

| 旧位置 | v2.0 位置 |
|--------|----------|
| `design_document_review_and_e2e.md` §4.1-4.6 日志 / metrics | 本文 §1 |
| `design_document_review_and_e2e.md` §4.7 告警规则 | 本文 §3 |
| `design_document_review_and_e2e.md` §4.8 优雅停机 | 本文 §10 |
| `design_document.md` §14 部署方案 | 本文 §6 §11 |

---

## 1. 可观测性三柱

### 1.1 Metrics（Prometheus）

#### 1.1.1 命名规范

```
{namespace}_{component}_{name}_{unit}
```

例：`dlw_controller_subtask_assignments_total`、`dlw_executor_chunk_download_bytes_total`、`dlw_source_request_duration_seconds`。

#### 1.1.2 核心指标清单

```yaml
# 任务级
- dlw_tasks_created_total{tenant, source_strategy}
- dlw_tasks_completed_total{tenant, status}      # status=completed/failed/cancelled
- dlw_task_duration_seconds_bucket{tenant}        # histogram，含 +Inf
- dlw_task_total_bytes_total{tenant}              # counter

# 子任务 / chunk
- dlw_subtasks_assigned_total{executor_id, source_id}
- dlw_subtask_assignment_failures_total{reason}    # cas_failed / no_capacity / quota
- dlw_chunk_download_bytes_total{executor, source}  # counter
- dlw_chunk_download_duration_seconds_bucket{source}
- dlw_chunk_download_speed_bytes_per_sec{executor, source}  # gauge

# 来源（多源）
- dlw_source_health{source_id}                     # 1=healthy, 0.5=degraded, 0=down
- dlw_source_request_total{source_id, status_code}
- dlw_source_429_rate_5m{source_id}                # gauge
- dlw_source_5xx_rate_5m{source_id}
- dlw_source_speed_ewma_bytes_per_sec{executor, source}

# 执行器
- dlw_executors_count{status}                      # gauge by status
- dlw_executor_health_score{executor_id}            # gauge 0..100
- dlw_executor_disk_free_gb{executor_id}
- dlw_executor_parts_dir_bytes{executor_id}

# 控制器
- dlw_controller_db_query_duration_seconds_bucket{query_type}
- dlw_controller_scheduler_loop_duration_seconds_bucket
- dlw_controller_heartbeat_processed_total{result}  # ok/stale/replay
- dlw_controller_active_ws_connections{tenant}

# 错误 / 重试
- dlw_subtask_retries_total{reason}                 # network/checksum/disk/throttle
- dlw_recovery_routine_actions_total{action}        # reset_pending/abort_multipart/...

# 配额（详见 04 §7）
- dlw_quota_bytes_used_month{tenant}               # gauge
- dlw_quota_exceeded_total{tenant, metric}

# 成本（详见 §8）
- dlw_egress_bytes_total{region_pair, source_id}
- dlw_storage_put_total{storage_id}
```

🔒 **基数控制（解决 OPS-15）**：

- ❌ **不要**：`{task_id="<uuid>"}`、`{subtask_id=...}`、`{filename=...}` —— UUID/无界字符串作 label 会爆炸 cardinality
- ✅ task / subtask 维度只在日志、trace exemplars 中出现

详见 04 §10 多租户 metrics 基数策略。

### 1.2 Logs（structlog + Loki）

#### 1.2.1 Schema 约束

```python
class LogEvent(TypedDict):
    timestamp: str          # RFC3339 UTC
    level: str              # debug/info/warn/error/critical
    component: str          # controller/executor/scheduler/proxy/...
    event: str              # 简短事件名，如 "subtask.assigned"
    trace_id: NotRequired[str]
    span_id: NotRequired[str]
    tenant_id: NotRequired[int]
    task_id: NotRequired[str]
    subtask_id: NotRequired[str]
    executor_id: NotRequired[str]
    error_code: NotRequired[str]
    duration_ms: NotRequired[float]
    # 业务字段...
```

#### 1.2.2 必带字段约束

CI lint 检查所有 `log.*()` 调用：

- `controller` 组件下：必须含 `trace_id`
- 任何任务相关日志：必须含 `task_id` 或 `subtask_id`
- 任何 executor 相关日志：必须含 `executor_id`
- 任何 source 相关：必须含 `source_id`

#### 1.2.3 Loki 查询样例

```logql
# 单任务全链路
{component=~"controller|executor|scheduler"}
  | json
  | task_id="abc-uuid"
  | line_format "{{.timestamp}} {{.component}} {{.event}}"

# 高频错误
sum by (error_code) (
  rate({level="error"} | json [5m])
)
```

#### 1.2.4 脱敏（详见 04 §9.3）

`structlog` processor 自动 redact `hf_*`, `AKIA*`, `Bearer *`。

### 1.3 Traces（OpenTelemetry，解决 OPS-06）

#### 1.3.1 真正埋点（不是只有 trace_id 字段）

```python
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor

# 启动时自动埋点
FastAPIInstrumentor.instrument_app(app)
HTTPXClientInstrumentor().instrument()
PsycopgInstrumentor().instrument()

# 关键 span 手动标注
@tracer.start_as_current_span("scheduler.assign_subtask")
def assign_subtask(...):
    span = trace.get_current_span()
    span.set_attribute("executor.id", executor_id)
    span.set_attribute("subtask.id", str(subtask_id))
    ...
```

#### 1.3.2 关键 span 列表

| Span name | 何时创建 | 关键 attrs |
|-----------|---------|-----------|
| `task.create` | POST /api/tasks | tenant_id, repo_id, revision |
| `scheduler.loop` | 每次调度 tick | duration, candidates_found |
| `scheduler.assign_subtask` | 单次 CAS | executor_id, subtask_id, cas_result |
| `heartbeat.process` | 心跳处理 | executor_id, subtask_count |
| `subtask.download` | executor 下载 | source_id, bytes, duration |
| `subtask.upload` | S3 multipart | parts, total_bytes |
| `subtask.verify_local` | 本地 SHA256 | duration |
| `subtask.verify_remote` | 远端 SHA256 | duration |
| `hf_proxy.request` | Controller HF 反代 | upstream_status |
| `recovery.routine` | 启动恢复 | actions_count |

#### 1.3.3 Trace ↔ Logs ↔ Metrics 关联

- Logs：每行带 `trace_id` + `span_id`
- Metrics：用 Prometheus exemplars（histogram bucket 关联到一个 trace_id）
- Grafana：单一界面跳转 metrics → trace → logs

#### 1.3.4 后端

- Tempo（Grafana 系列）或 Jaeger
- Sampling：根因分析用 head sampling 1%；错误强制 100%
- 保留：14 天

---

## 2. SLI / SLO 定义（解决 OPS-01）

### 2.1 用户视角的 4 个核心 SLO

| SLI | 定义 | SLO 目标 | 时间窗口 |
|-----|------|---------|---------|
| **任务完成率** | `tasks_completed / (tasks_completed + tasks_failed)` | ≥ 99.0% | 7d rolling |
| **下载吞吐** | `chunk_download_bytes_total / chunk_download_duration_seconds` ≥ 单 executor 链路 60% | 单任务 P95 满足 | 30d rolling |
| **API 可用性** | `2xx + 3xx / total` of `/api/tasks*`, `/api/models/search` | ≥ 99.9% | 30d rolling |
| **E2E 任务时延** | task_duration P95 ≤ predicted_eta × 1.5 | P95 满足 | 30d rolling |

### 2.2 Error Budget

```
Monthly error budget = 1 - SLO target
- 99.0% → 7.2h/month
- 99.9% → 43min/month
```

实施工具：[Pyrra](https://github.com/pyrra-dev/pyrra) 或 [Sloth](https://github.com/slok/sloth) 自动生成 recording rules。

```yaml
# slos.yaml
service: dlw-controller
slos:
  - name: api_availability
    objective: 99.9
    description: "Public API availability"
    sli:
      events:
        error_query: 'rate(http_requests_total{status_code=~"5..", endpoint=~"/api/.*"}[5m])'
        total_query: 'rate(http_requests_total{endpoint=~"/api/.*"}[5m])'
    alerting:
      page_alert:
        annotations:
          summary: "API SLO burning fast"
      ticket_alert:
        annotations:
          summary: "API SLO burning slowly"
```

### 2.3 Per-Tenant SLO

企业版可按租户报告 SLO（合同绑定）：

```promql
sum by (tenant_id) (rate(dlw_tasks_completed_total{status="completed"}[7d]))
/ sum by (tenant_id) (rate(dlw_tasks_completed_total[7d]))
```

---

## 3. 告警分级与抑制（解决 OPS-07 / OPS-08）

### 3.1 三档分级

| Severity | 路由 | 响应时间 | 例子 |
|----------|------|---------|------|
| **P0 (page)** | PagerDuty / 电话 | 5min | ControllerDown / DataIntegrityFailure / AllExecutorsOffline / CertExpiringIn1h |
| **P1 (work-hours)** | Slack #alerts | 1h | HighErrorRate / HFGlobalThrottle / SLOBurnFast |
| **P2 (ticket)** | Jira | 1d | DiskSpaceLow / ExecutorDegraded / SLOBurnSlow |

### 3.2 Alertmanager 配置

```yaml
route:
  group_by: [alertname, tenant_id]
  group_interval: 5m
  repeat_interval: 4h
  receiver: 'slack-default'
  routes:
    - matchers: [severity="page"]
      receiver: pagerduty
      group_wait: 0s
    - matchers: [severity="ticket"]
      receiver: jira
      group_wait: 1h

inhibit_rules:
  # ControllerDown 抑制下游
  - source_matchers: [alertname="ControllerDown"]
    target_matchers: [alertname=~"TaskStuck|HighErrorRate|HFGlobalThrottle"]
    equal: []
  # AllExecutorsOffline 抑制单 executor 告警
  - source_matchers: [alertname="AllExecutorsOffline"]
    target_matchers: [alertname=~"ExecutorOffline|ExecutorDegraded"]
    equal: []
```

### 3.3 Hysteresis（触发与恢复阈值不对称）

```yaml
# Bad: 50 上下抖动产生告警风暴
# Good: 触发=50, 恢复=70

- alert: ExecutorHealthLow
  expr: dlw_executor_health_score < 50
  for: 5m
- alert_recovery: ExecutorHealthLow
  expr: dlw_executor_health_score > 70
  for: 5m
```

实施：用 PrometheusRule + recording rule，引入 `is_alerting` gauge 显式建状态。

### 3.4 关键告警清单

| Alert | severity | 触发条件 | Runbook |
|-------|----------|---------|---------|
| ControllerDown | page | up{job="dlw-controller"} == 0 for 1m | RB-01 |
| ControllerHighRestart | page | rate(...restart_total[15m]) > 0 | RB-01 |
| AllExecutorsOffline | page | count(dlw_executors_count{status="healthy"}) == 0 | RB-02 |
| DataIntegrityFailure | page | dlw_subtask_retries_total{reason="checksum"} burst | RB-03 |
| CertExpiringIn1h | page | mtls_cert_expiry_seconds < 3600 | RB-08 |
| HFGlobalThrottle | work-hours | dlw_source_429_rate_5m{source_id="huggingface"} > 0.05 for 5m | RB-04 |
| HFCompletelyDown | work-hours | dlw_source_health{source_id="huggingface"} == 0 for 10m | RB-04 |
| SLOBurnFastAvailability | work-hours | API 99.9% SLO 1h burn rate > 14.4 | RB-09 |
| SLOBurnSlowAvailability | ticket | 6h burn rate > 1 | RB-09 |
| StorageS3High5xx | work-hours | rate(storage_put_failures[10m]) > 0.05 | RB-05 |
| ExecutorDegraded | ticket | dlw_executor_health_score < 50 for 10m | RB-02 |
| DiskSpaceLow | ticket | dlw_executor_disk_free_gb < 50 | RB-06 |
| PartsDirHigh | ticket | parts_dir_bytes / disk_total > 0.6 | RB-07 |
| QuotaApproachingLimit | ticket | bytes_used_month / quota > 0.85 | RB-10 |

---

## 4. Runbook 集（解决 OPS-02）

> 存放在 `docs/runbooks/`，每个 Runbook 一份 markdown，5min 内可读完。
> 每条告警 annotation 必须含 runbook_url。

### 4.1 RB-01 — Controller 故障

**症状**：`ControllerDown` page

**步骤**：

```
1. 立即 ssh 到 controller standby
2. 检查 standby 状态：
   $ systemctl status dlw-controller-standby
   $ pg_isready -h localhost
3. 确认 active 不可达：
   $ curl https://controller-active.dlw/health
4. 提升 standby（详见 §6.1）
5. 切换 DNS / VIP：
   $ ./scripts/promote-standby.sh
6. 验证新 active：
   $ curl https://controller.dlw/health
   # 应返回 {"status": "healthy", "role": "active"}
7. 检查 recovery_routine 完成（log "recovery routine complete"）
8. 通知 stakeholders（Slack #incidents）
9. 创建事故复盘单（24h 内）
```

**RTO 目标**：≤ 10min

### 4.2 RB-02 — Executor 替换 / 全部下线

**单 executor 替换**：

```
1. 检查执行器状态：
   $ kubectl get pods -l app=dlw-executor
2. 如果 NotReady > 5min，drain：
   $ ./scripts/drain-executor.sh <executor-id>
   # drain 行为：
   #   - controller 标该 executor 为 'maintenance'
   #   - 不再分配新 subtask
   #   - 等 in-flight subtask 完成（最长 1h）
   #   - 超时则把 in-flight subtask 写回 DB pending，让其他 executor 接管
3. 删除 pod，让 deployment 重建
4. 新 executor 自动 register，拿到新 epoch
```

**全部下线**：

```
1. 立即 page on-call manager
2. 检查 controller 是否健康（不健康先按 RB-01）
3. 检查网络层：执行器是否能 ping controller
   $ kubectl exec -it <pod> -- curl https://controller.dlw/health
4. 检查 enrollment_secret / mTLS 证书是否过期：
   $ ./scripts/check-mtls-validity.sh
5. 检查 K8s 节点：是否有节点驱逐 / OOM
6. 如果是凭证问题，按 RB-08
```

### 4.3 RB-03 — DataIntegrityFailure（SHA256 不匹配）

```
1. 立即调查触发的 subtask：
   SELECT id, task_id, filename, source_id, expected_sha256, actual_sha256
   FROM file_subtasks
   WHERE status='failed_permanent' AND last_error LIKE '%CHECKSUM%'
   ORDER BY created_at DESC LIMIT 20;
2. 检查 source_id 分布：是否集中在某一个源？
   - 若是单一源 → 临时拉黑该源（admin UI），可能源被污染
   - 若分布在多源 → HF 上游可能有问题，等 HF 修复
3. 检查 fingerprint 表是否有不一致历史：
   SELECT * FROM file_fingerprints WHERE repo_id=... AND filename=...;
4. 如果是自托管 mirror：检查 mirror 同步是否最新
5. 通知用户重建任务（用具体 sha 而非 main）
```

### 4.4 RB-04 — HF 限流 / 不可达

```
1. 检查全局状态：
   SELECT * FROM source_throttle_state WHERE source_id='huggingface';
2. 自动降级触发：
   - throttled → 已经在 50% 速率
   - circuit_open → 已经停下
3. 切换源：通知用户改用 source_strategy='pin_modelscope' 或 'pin_hf_mirror'
4. 若 HF 全网下：
   - paused_external 任务自动等
   - 检查 https://status.huggingface.co
5. 恢复后：
   - 等下个状态机评估周期（5min），自动回到 normal
   - 或手动：UPDATE source_throttle_state SET state='normal', next_review_at=now()
```

### 4.5 RB-05 — Storage（S3 / OBS）异常

```
1. 检查最近 PUT 失败：
   sum by (storage_id, status_code) (rate(dlw_storage_put_total[5m]))
2. 测试 storage 可达：
   $ aws s3 ls s3://bucket/  --endpoint-url=...
3. 检查凭证：
   - STS issuance 是否在 expires_at 之内
   - long-term credential 是否被 rotate
4. 5xx 短期：观察 5min；多数 cloud provider 自愈
5. 5xx 长期：
   - 暂停接收新任务（admin UI 系统设置）
   - 等 cloud provider 通告
   - 切换到 fallback storage backend（详见 RB-11）
```

### 4.6 RB-06 — Disk Space Low

```
1. 检查 .parts/ 占用：
   $ du -sh /var/lib/dlw/parts/
2. 触发 GC：
   $ ./scripts/gc-orphan-parts.sh
   # 删除超过 24h 无引用的 .parts/
3. 检查未清理的 subtask:
   SELECT executor_id, count(*), sum(bytes_downloaded)
   FROM file_subtasks WHERE executor_id='X' AND status='failed_permanent'
   GROUP BY executor_id;
4. 如果 GC 不够，扩容磁盘（修改 PVC size）
5. 极端情况：标 executor 'maintenance'，drain，迁移 in-flight
```

### 4.7 RB-07 — Parts Dir 占用过高

参见 RB-06。

### 4.8 RB-08 — Token / Cert 轮换

#### HF Token 轮换

```
1. 在 HF 创建新 token，作为 secondary
2. 通过 admin API 添加：
   POST /api/admin/tenants/{id}/hf-tokens
   {"value": "<new_token>", "is_primary": false}
3. 等 5min（让所有 controller pod 拉到新 secret）
4. 切换 primary：
   POST /api/admin/tenants/{id}/hf-tokens/<id>/promote
5. 监控错误率 30min
6. 删除老 token
```

#### mTLS 证书轮换

```
# 自动续签：每 12h 自动续签即将过期的证书
# 手动触发：
$ ./scripts/rotate-executor-mtls.sh <executor-id>
# 流程：
# 1. controller 签发新证书
# 2. executor 收到新证书后切换
# 3. 5min grace period 期间老证书仍有效
```

### 4.9 RB-09 — SLO 燃烧

```
1. 打开 Grafana SLO dashboard
2. 看哪个 SLI 在燃烧
3. 对应 runbook：
   - API 可用性 → RB-01 / RB-05
   - 任务完成率 → 看 failure 分布（按 reason）
   - 时延 → 看哪个 source / executor 拖慢
4. 评估剩余 error budget；不足时冻结新发布
```

### 4.10 RB-10 — 配额接近上限

```
1. 通知 tenant_admin
2. 用户视角：
   GET /api/quota/current
3. 选项：
   a. 临时提额（admin 操作）
   b. 等下月重置
   c. 升级套餐（联系销售）
4. 已 hard_block 的任务：会得到 429，需用户重试
```

### 4.11 RB-11 — 存储后端切换

```
1. 评估：当前 storage 不可恢复 → 必须切换
2. 创建新 storage backend：
   POST /api/admin/storage-backends
3. 更新受影响 task 的默认 storage：
   UPDATE projects SET storage_id=<new> WHERE storage_id=<old>;
   （注意：仅影响新任务；进行中任务不切换）
4. 评估是否需要数据迁移（已下完的内容是否拷贝到新 storage）
   - 拷贝：用 rclone / aws s3 sync
5. 退役老 storage 前等 90 天（合规保留期）
```

### 4.12 RB-12 — 优雅停机 / 计划维护

```
1. 提前 24h 通知用户（webhook + UI banner）
2. 进入 maintenance 模式：
   POST /api/admin/maintenance/enter {"freeze_minutes": 30}
   # 行为：
   #   - 拒绝创建新任务（429）
   #   - 进行中任务继续
   #   - 不影响心跳
3. 等待 in-flight 完成或到 timeout
4. 执行维护
5. 退出：POST /api/admin/maintenance/exit
```

---

## 5. 备份与灾难恢复（解决 OPS-03）

### 5.1 RPO / RTO 目标

| 资产 | RPO | RTO |
|------|-----|-----|
| Controller PG | 15 分钟 | 10 分钟 |
| 已 verified 文件（在 storage） | 0（依赖 storage 后端 SLA） | 0 |
| .parts/ 临时区 | 不备份（可重下） | 0 |
| 配置（sources.yaml, 凭证） | 24h | 30 分钟 |
| 审计日志 | 0（每条同步写 WORM） | 取决于 WORM 提取时间 |

### 5.2 PostgreSQL 备份策略

```bash
# WAL archive（连续）
archive_command = 'aws s3 cp %p s3://dlw-backup/wal/%f'
# 每 6 小时全量
0 */6 * * * /usr/bin/pg_basebackup -D /backup/$(date +%Y%m%d-%H) -F tar -z -P
# 每日 verify
0 4 * * * /scripts/verify-backup.sh
```

PITR（point-in-time-recovery）测试：每月 1 次，dev 环境 restore 到 15min 前的状态。

### 5.3 .parts/ 持久卷

不再用 `/tmp`：

```yaml
# k8s
volumeClaimTemplates:
  - metadata:
      name: dlw-parts
    spec:
      accessModes: [ReadWriteOnce]
      resources:
        requests:
          storage: 500Gi
      storageClassName: fast-ssd
```

启动 GC 详见 RB-06。

### 5.4 配置备份

`sources.yaml`、租户配置、license 策略等：

- Git 版本控制（pull request 流程）
- 部署到 controller 通过 ConfigMap reload

---

## 6. 灰度发布与在线升级（解决 OPS-04）

### 6.1 Controller active/standby（v1 必备）

```
                  ┌──────────────┐
                  │     LB       │
                  └──────┬───────┘
                         │
              ┌──────────┼──────────┐
              │                     │
          ┌───▼───┐             ┌───▼───┐
          │active │ ──── PG ───►│standby│
          │       │  streaming  │       │
          └───────┘             └───────┘
```

部署：

- 两个 controller 实例，PG streaming replication
- LB 健康检查 `/health/active`，仅 active 返回 200
- pg_advisory_lock 确保唯一 active：`SELECT pg_try_advisory_lock(<dlw_active_lock_id>)`
- standby 启动时拿不到锁 → 启动 hot replay 但不接流量
- active 崩溃 / 锁释放 → standby 拿到锁 → 自动 promote

📝 **决策**：v2.0 不做 active-active（多 active 调度协调复杂）。详见 06 §9 roadmap。

### 6.2 Executor 滚动升级

```yaml
# K8s Deployment
strategy:
  type: RollingUpdate
  rollingUpdate:
    maxUnavailable: 1
    maxSurge: 1
```

executor SIGTERM 行为（解决 OPS-04）：

```python
async def graceful_shutdown(signum, frame):
    # Step 1: 进入 'draining' 状态，告知 controller
    await heartbeat_with_status('draining')

    # Step 2: 不再接受新 subtask（心跳里 capacity=0）
    state.accept_new = False

    # Step 3: 已开始的 in-flight subtask
    deadline = time.monotonic() + GRACEFUL_TIMEOUT  # 默认 600s
    while state.running_subtasks and time.monotonic() < deadline:
        await asyncio.sleep(5)

    # Step 4: 仍未完成的：把 in-flight subtask 状态写回 DB → pending
    # 让 controller 重新分配给其他 executor（不是 v1.x 的"等 60s 然后失败"）
    for s in state.running_subtasks:
        await state.persist_inflight(s)
        await controller.release_subtask(s.id, current_epoch)

    # Step 5: 退出
    sys.exit(0)
```

### 6.3 灰度策略

```
1. 在 1 个 dev cluster 部署 candidate
2. 跑 E2E 测试（详见 06 §8）
3. 在 staging cluster 灰度 10% executor
4. 观察 30min 关键 metrics（错误率、speed、health_score）
5. 50% → 100%（每步观察）
6. Controller 升级：先 standby，验证后切换 → 再升级老 active
```

### 6.4 在线 schema 迁移

强制使用扩展性 migration（不锁表）：

```sql
-- 加列：默认 NULL，应用代码先支持读（兼容期）
ALTER TABLE file_subtasks ADD COLUMN executor_epoch BIGINT;
-- 然后填充：在小事务中
UPDATE file_subtasks SET executor_epoch=0 WHERE executor_epoch IS NULL;
-- 最后约束：
ALTER TABLE file_subtasks ALTER COLUMN executor_epoch SET NOT NULL;
```

工具：[squawk](https://github.com/sbdchd/squawk) lint 高风险 migration。

---

## 7. 容量规划（解决 OPS-09）

### 7.1 量化模型

**单 controller**：

| 资源 | 上限 | 瓶颈 |
|------|------|------|
| Heartbeat 处理 | ~5000 ops/s | PG 写（10s 心跳 × 1000 executor / 200ms 合并 = 5000 commits/s） |
| WS broadcast | ~200 client × 100KB/s = 20MB/s | 网络出口 |
| API QPS | ~2000 r/s | DB 查询 |
| 实际推荐 executor 数 | ≤ 1000 per controller | 上述综合 |

**单 executor**：

- NIC 10Gbps → 1.25GB/s 理论
- 实际持续：~1GB/s（多 chunk 并发）
- 同 host 多 executor：总和不超过 NIC 80%

**Storage（S3）**：

- 写入 QPS：3500 PUT/s/prefix（默认；可申请提升）
- 单 multipart：每个 part 5MB-5GB；总 part 数 ≤ 10000

### 7.2 扩容触发

| 指标 | 阈值 | 动作 |
|------|------|------|
| Controller CPU | > 70% sustained 30min | 升级硬件 / 切 vertical scale |
| Controller PG IOPS | > 80% IOPS limit | upgrade IOPS / split DB |
| Executor count | > 800 | 部署第二套 controller cluster（按 tenant 分组） |
| Egress bandwidth | > 80% link capacity | 与 ISP 谈扩容 |

### 7.3 多 controller cluster（横向扩展）

> v2.1+ 才需要

按 tenant 分片：

```yaml
clusters:
  cluster-a: tenants [1-100]
  cluster-b: tenants [101-200]
```

UI 通过统一入口（path-based routing）路由到对应 cluster。

---

## 8. 成本模型与控制（解决 OPS-10）

### 8.1 成本来源

| 项目 | 单位 | 单价（参考） | 主要发生地 |
|------|------|------------|-----------|
| 跨 region egress | GB | $0.02-$0.09 | AWS / GCP |
| Storage PUT | 1k req | $0.005 | S3 |
| Storage GET | 1k req | $0.0004 | S3 |
| Storage 持久 | GB-month | $0.023 | S3 standard |
| KMS encrypt | 10k req | $0.03 | AWS KMS |
| Compute（controller） | hour | $0.10-$0.50 | EC2 c5 |

### 8.2 成本 metrics

```
dlw_egress_bytes_total{src_region, dst_region, source_id}
dlw_storage_put_total{storage_id, bucket}
dlw_storage_size_bytes{storage_id, tenant}
dlw_kms_calls_total{operation}
```

预算面板（Grafana）：

```
月度成本估算 = sum(egress × $/GB) + sum(PUT × $/1k) + sum(storage × $/GB-month)
```

### 8.3 控制旋钮

| 旋钮 | 实施 | 节省 |
|------|------|------|
| Region affinity 调度 | 优先把任务分配给与 storage 同 region 的 executor | 跨 region egress |
| 内置 mirror（hf-mirror, modelscope） | 中国境内首选 | HF egress |
| 自托管 mirror | 内网 → S3 同 region | 100% egress 省 |
| Storage tiering | 90 天未访问 → IA → Glacier | storage |
| Multipart part size | 调到 64MB+ 减少 PUT 次数 | PUT |
| KMS DEK 缓存 | 5min 内复用 | KMS |
| WS delta 限频 | 100ms 合并 | controller 出网 |

### 8.4 Per-Tenant Chargeback

详见 04 §7.6。

---

## 9. Chaos / DR 演练（解决 OPS-14）

### 9.1 季度演练计划

| 演练 | 工具 | 验证 |
|------|------|------|
| 杀 controller-active | chaos-mesh | RTO ≤ 10min |
| 模拟 HF 全局 429 | nginx upstream block | paused_external 触发 |
| 删除单 executor 的 .parts/ | rm -rf | resume 自动恢复 |
| PG primary 故障 | systemctl stop | streaming replica 提升 |
| 网络分区（exec ↔ controller） | iptables | reclaim + reassign 工作 |
| Storage 写失败 | aws cli with bad creds | 任务进 paused_external |
| 时钟漂移 | date -s "..." | NONCE_REPLAY / CLOCK_SKEW 错误 |

### 9.2 GameDay

每半年 1 次 GameDay：on-call 团队应对未通知的故障注入，记录 MTTR / MTTD。

---

## 10. 优雅停机（解决 OPS-04）

详见 §6.2。补充：

### 10.1 Controller 优雅停机

```python
async def controller_shutdown():
    # 1. 健康检查返回 not-ready，LB 摘除流量
    state.health = 'draining'
    await asyncio.sleep(LB_DRAIN_DELAY)  # 默认 30s 给 LB 反应

    # 2. 关闭 WS 连接（让 client 重连到其他实例）
    for ws in active_ws:
        await ws.close(code=1012, reason="server_restart")

    # 3. 拒绝新 API 请求
    state.accept_new = False
    # 进行中的请求等完成（最长 60s）

    # 4. 心跳处理继续到 timeout（让 executor 不立即触发故障）
    await wait_for_heartbeats_drain(timeout=120)

    # 5. 释放 advisory lock（standby 自动接管）
    await db.execute("SELECT pg_advisory_unlock(...)")

    # 6. 关闭 DB 连接池
    await db.close()
```

### 10.2 临时状态文件保护

`.parts/` 中的 chunk 状态文件用 atomic write：

```python
def save_state(path: Path, data: bytes):
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_bytes(data)
    os.fsync(tmp.fd)
    tmp.replace(path)            # atomic rename
```

加密（解决 SEC-13）：详见 04 §3.3。

---

## 11. 部署拓扑

### 11.1 推荐生产部署（K8s）

```yaml
# helm values.yaml 概要
controller:
  replicas: 2               # active + standby
  resources:
    cpu: 4
    memory: 8Gi
  persistence:
    pg:
      storage: 500Gi
      iops: 5000
      backup:
        wal_archive_bucket: s3://dlw-backup/wal
        basebackup_schedule: "0 */6 * * *"

executors:
  count: 10
  resources:
    cpu: 4
    memory: 8Gi
  persistence:
    parts:
      storage: 500Gi      # ≥ 1.5x 最大单文件
      storageClassName: fast-ssd

observability:
  prometheus:
    retention: 30d
    remote_write: thanos
  loki:
    retention: 90d
  tempo:
    retention: 14d
  grafana:
    enabled: true
    sso: oidc

ingress:
  ui:
    host: dlw.example.com
    tls: cert-manager
  api:
    host: api.dlw.example.com
```

### 11.2 单机开发部署（docker-compose）

```yaml
# 不适合生产
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: dlw
  controller:
    image: dlw/controller:v2.0
    depends_on: [postgres]
  executor:
    image: dlw/executor:v2.0
    deploy:
      replicas: 2
```

---

## 12. 与其他文档的链接

- 状态机 / 数据模型：→ [01-architecture.md](./01-architecture.md)
- API 与心跳：→ [02-protocol.md](./02-protocol.md)
- 分布式正确性：→ [03-distributed-correctness.md](./03-distributed-correctness.md)
- 安全 / 多租户 / 审计：→ [04-security-and-tenancy.md](./04-security-and-tenancy.md)
- 多源 metrics（按 source_id label）：→ [06-platform-and-ecosystem.md](./06-platform-and-ecosystem.md) §1
