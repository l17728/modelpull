# 03 — 分布式正确性

> 角色：分布式深 dive，给后端实现者与审视者。
> 取代：v1.0 §5.1.3 / §10，v1.4 §1 §2 §4，v1.5 §1。

---

## 0. 从历史文档迁移指引

| 旧位置 | v2.0 位置 |
|--------|----------|
| `design_document.md` §10 完整性校验 | 本文 §3 §6 |
| `design_document_fault_tolerance_and_visualization.md` §1 故障治愈 | 本文 §5 §9 |
| `design_document_fault_tolerance_and_visualization.md` §2 多级重试 | 本文 §8 |
| `design_document_review_and_e2e.md` §1.1-1.4 Review 修复 | 本文（按主题归类） |

---

## 1. 分布式假设与故障模型

### 1.1 系统模型

- **Fail-stop**：节点崩溃后不再产生消息（不会发出错误的"陈旧"消息）
- **部分同步**：消息延迟有上界但未知；时钟有界漂移（≤60s，由心跳协议强制）
- **拜占庭**：默认不防御（私有部署假设）；多源 SHA256 校验是次要防线（详见 04 §5）
- **持久化**：PostgreSQL 单一权威，WAL fsync 后视为已提交
- **Network**：可能分区、延迟、丢包；不假设"网络一定恢复"

### 1.2 故障预算

| 故障 | 期望可用性 | 关键不变量 |
|------|-----------|-----------|
| 单 Executor 崩溃 | 100% recoverable | 已下载字节不丢 |
| Controller 崩溃 | RTO ≤ 10min, RPO ≤ 15min | DB 持久化的状态可恢复 |
| 网络分区（Executor↔Controller） | 任务暂停，恢复后继续 | 不双发，不丢数据 |
| HF / 源不可达 | 暂停，定期重试 | 不进 failed |
| Storage 5xx | 重试 + circuit break | 数据完整性 |
| 磁盘满 | 优雅停机，不腐败数据 | 已下载文件不删 |

### 1.3 关键不变量索引

来自 01 §7，与本章高度相关的：

- 不变量 5：任务级最终校验比对 sha256（不仅 size）
- 不变量 6：assigned → downloading 必带 assignment_token
- 不变量 7：每次 transition 写 history 表
- 不变量 9：(executor_id, epoch) 是因果时钟
- 不变量 11：HF 是 SHA256 真值来源

---

## 2. Fence Token + Executor Epoch（防双发的核心）

> 解决 D1 / D6（reviewer 提出的 Critical 问题）。

### 2.1 问题陈述

v1.5 的 CAS 修复仅保护 DB 层。但实际下发链路中存在多个时间点会"陈旧地"指挥执行器：

```
T1:  scheduler 在内存中决定把 S 分给 A
T2:  controller 把 S 写入 A 的 HeartbeatResponse 队列
T3:  A 网络抖动，T2 的响应丢失
T4:  controller 等 60s 心跳超时 → 标 A faulty → reclaim S → 重分给 B
T5:  A 网络恢复，重发心跳
T6:  controller "继续工作"，但 A 不知道 S 已被回收，开始下载 S
T7:  B 也下载 S → 双下载、双上传
```

CAS 在 T1-T2 防止了"两个调度器线程同时分配"，但防不了 T5-T6 的"陈旧 executor 拿陈旧任务"。

### 2.2 协议设计

引入两层 fence：

**第一层：Executor Epoch**（每个 executor 一个）

- Controller 重启 / Executor register / forced re-register 时，分配新 epoch（单调递增）
- 所有 Executor 的请求必须携带其 epoch
- 服务端验证：`request.epoch == executors.epoch WHERE id = executor_id`，不匹配返回 `401 EPOCH_MISMATCH`
- Executor 收到 401 后立即停止所有 in-flight subtask，重新 register

**第二层：Assignment Token**（每个 subtask assignment 一个）

- 每次 `assign_subtask` 生成新 UUID 写入 `file_subtasks.assignment_token`
- Response 中带给 executor
- Executor 在 download / upload / complete 三阶段都携带原 token
- 服务端每次操作前 `WHERE id=? AND assignment_token=?`

### 2.3 CAS-then-enqueue 实现

```python
def assign_subtask(executor_id: str, executor_epoch: int) -> AssignmentResult | None:
    """
    Atomically assign a pending subtask to executor.
    Returns assignment with fresh token, or None if no work.

    必须在 SCHEDULER_LOCK 内调用，防止 N 个线程同时进入这段代码。
    """
    with db.transaction():
        # Step 1: 验证 executor epoch（防 stale executor）
        cur = db.execute(
            "SELECT epoch, status FROM executors WHERE id = %s FOR UPDATE",
            (executor_id,),
        )
        row = cur.fetchone()
        if not row or row.epoch != executor_epoch or row.status not in ("healthy", "probationary"):
            raise StaleExecutor()

        # Step 2: 选 candidate（按调度策略）
        candidate = pick_candidate_subtask(executor_id)  # 已含 LOCK FOR UPDATE SKIP LOCKED
        if not candidate:
            return None

        # Step 3: 原子 CAS-then-enqueue
        new_token = uuid.uuid4()
        rowcount = db.execute(
            """
            UPDATE file_subtasks
            SET status = 'assigned',
                executor_id = %s,
                executor_epoch = %s,
                assignment_token = %s,
                assigned_at = now()
            WHERE id = %s
              AND status = 'pending'
              AND (executor_id IS NULL OR executor_id = %s)
            """,
            (executor_id, executor_epoch, str(new_token), candidate.id, executor_id),
        ).rowcount

        if rowcount == 0:
            # 别的 thread 抢先了，或 candidate 状态变了
            return None  # 调用方循环重试

        # Step 4: 立即 commit；此后才能 enqueue 到 response
        # （事务结束）

    # Step 5: 仅在事务成功 commit 后才返回 → 才会被塞入 HB response
    return AssignmentResult(
        subtask_id=candidate.id,
        assignment_token=new_token,
        executor_epoch=executor_epoch,
    )
```

🔒 **不变量**：`enqueue_for_response()` **必须**在 `db.commit()` 之后调用。否则若 commit 失败但 enqueue 已发生，就回到 D1 漏洞。

### 2.4 Reclaim 时的 fence 行为

当 controller 检测到 executor 心跳超时 → 触发 reclaim：

```python
def reclaim_subtasks(executor_id: str, current_epoch: int):
    """
    Reclaim subtasks owned by executor. 用 current_epoch 做 fence，
    防止旧 epoch 的 reclaim 调用覆盖新 epoch 的工作。
    """
    db.execute(
        """
        UPDATE file_subtasks
        SET status = 'pending',
            executor_id = NULL,
            executor_epoch = NULL,
            assignment_token = NULL,
            retry_count = retry_count + 1
        WHERE executor_id = %s
          AND executor_epoch = %s         -- 关键：epoch 校验
          AND status IN ('assigned', 'downloading', 'uploading')
        """,
        (executor_id, current_epoch),
    )
```

**关键属性**：

- 如果 executor 已经在 reclaim 之间重新 register（拿到 new epoch）且开始新工作，旧 epoch 的 reclaim 不会清掉新 assignment（WHERE 条件不匹配）
- 但如果 executor 完全失联，reclaim 正常生效

### 2.5 Complete 上报的 fence

```python
def complete_subtask(
    executor_id: str, subtask_id: UUID,
    executor_epoch: int, assignment_token: UUID,
    actual_sha256: str, ...
):
    rowcount = db.execute(
        """
        UPDATE file_subtasks
        SET status = 'verifying_remote',
            actual_sha256 = %s,
            ...
        WHERE id = %s
          AND executor_id = %s
          AND executor_epoch = %s
          AND assignment_token = %s
          AND status IN ('uploading', 'verifying_local')
        """,
        ...
    ).rowcount

    if rowcount == 0:
        raise StaleAssignment("subtask was reclaimed or already completed")
```

### 2.6 双发情况下的最坏行为

即使协议正确实现，也可能发生"双下载"：

```
T1: A 拿到 S（token=T1）
T2: A 失联，T1 仍下载中
T3: reclaim → S 回 pending
T4: B 拿到 S（token=T2）
T5: A 与 B 同时下载  ← 这是允许的，浪费带宽但不腐败数据
T6: A 完成，complete API 用 token=T1 → 服务端 WHERE token=T2 → 拒绝
T7: B 完成，complete API 用 token=T2 → 服务端接受
```

✅ 数据正确：B 的版本被 commit，A 的版本被丢弃（本地 .parts/ 由 GC 清理）。
⚠️ 已知开销：T5 期间 A 浪费的带宽。可接受，因 reclaim 仅在心跳超时 30-60s 后触发。

---

## 3. 崩溃恢复语义

### 3.1 控制器崩溃恢复

启动时执行 `recovery_routine()`：

```python
def recovery_routine():
    """
    必须在接受任何新心跳/请求之前完成。
    """
    # Step 1: 修复任务级状态
    # 'verifying' 任务的所有 subtask 必须 'verified'，否则回退
    db.execute("""
        UPDATE download_tasks
        SET status = 'downloading'
        WHERE status = 'verifying' AND id IN (
            SELECT task_id FROM file_subtasks
            WHERE status NOT IN ('verified', 'cancelled', 'failed_permanent')
            GROUP BY task_id
        )
    """)

    # Step 2: 'cancelling' 任务的 in-flight 重新评估
    db.execute("""
        UPDATE file_subtasks SET status = 'cancelling'
        WHERE status IN ('assigned', 'downloading', 'uploading', 'verifying_local',
                         'verifying_remote')
          AND task_id IN (SELECT id FROM download_tasks WHERE status = 'cancelling')
    """)

    # Step 3: 对 'uploading' / 'verifying_remote' 的 subtask 做三联校验（详见 §3.2）
    in_flight_uploads = db.execute("""
        SELECT * FROM file_subtasks
        WHERE status IN ('uploading', 'verifying_remote')
    """)
    for s in in_flight_uploads:
        verify_remote_state(s)   # 见 §3.2

    # Step 4: 'assigned' / 'downloading' 的 subtask 重置为 pending
    # 必须在 §3.5 时间窗口内（避免 reset 正常运行的 executor 工作）
    threshold = now() - timedelta(seconds=120)  # 2x heartbeat interval
    db.execute("""
        UPDATE file_subtasks
        SET status = 'pending',
            executor_id = NULL,
            executor_epoch = NULL,
            assignment_token = NULL
        WHERE status IN ('assigned', 'downloading')
          AND (last_heartbeat_seen_at IS NULL OR last_heartbeat_seen_at < %s)
    """, (threshold,))

    # Step 5: 清理孤儿 multipart upload（见 §3.4）
    cleanup_orphan_multiparts()

    # Step 6: HF 全局 429 状态从 DB 恢复（见 §8）
    restore_global_throttle_state()

    log.info("recovery routine complete")
```

### 3.2 三联校验（解决 D2）

```python
def verify_remote_state(s: FileSubTask) -> None:
    """
    崩溃前 s 处于 uploading 或 verifying_remote。
    崩溃后我们不知道远端状态如何。必须查清楚再决定下一步。
    """
    storage = get_storage_backend(s.task.storage_id)
    remote_key = build_path(s.task.path_template, s.filename)

    # Check 1: 远端是否存在
    head = storage.head_object(remote_key)
    if not head:
        # 远端不存在 → 上传从未完成 → 回退
        # 处理 multipart：如果有 upload_id，abort 它
        if s.multipart_upload_id:
            try_abort_multipart(storage, remote_key, s.multipart_upload_id)
        s.status = 'pending'
        s.multipart_upload_id = None
        return

    # Check 2: size 匹配
    if head.size != s.expected_size:
        # 损坏的远端文件，可能 multipart complete 时只 commit 了部分
        try_abort_multipart(storage, remote_key, s.multipart_upload_id)
        storage.delete_object(remote_key)
        s.status = 'pending'
        s.multipart_upload_id = None
        return

    # Check 3: SHA256 匹配（用 S3 ChecksumSHA256 或下载远端文件计算）
    remote_sha = head.checksum_sha256 or compute_remote_sha256(storage, remote_key)
    if remote_sha != s.expected_sha256:
        # 内容损坏（极罕见，可能是分片合并 bug）
        storage.delete_object(remote_key)
        s.status = 'pending'
        return

    # 三项全过：远端确实是好的
    s.actual_sha256 = remote_sha
    s.status = 'verified'
    log.info(f"recovery: subtask {s.id} confirmed verified at remote")
```

🔒 **不变量**：恢复后任何 `verified` 状态都必须经过三项校验。绝不能仅凭"本地 .parts/ 存在"或"DB 标记 verifying" 推断 verified。

### 3.3 Executor 崩溃恢复

Executor 启动时：

```python
def executor_startup():
    # Step 1: 扫描 .parts/ 目录的所有未完成下载
    existing_parts = scan_parts_dir(PARTS_DIR)

    # Step 2: register（拿到新 epoch）
    register_response = register_with_controller()
    new_epoch = register_response.epoch

    # Step 3: 用旧的 .parts/ 和 controller 协商
    # Controller 知道哪些 subtask 仍归属本 executor（按 host_id + last 注册）
    my_active_subtasks = controller.list_my_active_subtasks(executor_id, new_epoch)

    # Step 4: matching
    for parts in existing_parts:
        if parts.subtask_id in my_active_subtasks:
            # 仍归我，可恢复下载（断点续传）
            resume_download(parts.subtask_id, parts.byte_offset)
        else:
            # 已被 reclaim 给别人 → 删除本地
            shutil.rmtree(parts.dir)

    # Step 5: 清理超过 24h 无引用的 .parts/
    gc_orphan_parts(PARTS_DIR, ttl_hours=24)
```

### 3.4 Multipart Upload 清理

```sql
-- 持久化每个 upload 的 multipart upload_id
ALTER TABLE file_subtasks ADD COLUMN multipart_upload_id VARCHAR(256);
ALTER TABLE file_subtasks ADD COLUMN multipart_started_at TIMESTAMPTZ;

-- 启动时孤儿清理
SELECT id, multipart_upload_id, ...
FROM file_subtasks
WHERE multipart_upload_id IS NOT NULL
  AND status NOT IN ('uploading', 'verifying_remote');
-- 这些是悬空 upload_id，需要 abort
```

```python
def cleanup_orphan_multiparts():
    candidates = db.fetchall("""
        SELECT id, multipart_upload_id, ... FROM file_subtasks
        WHERE multipart_upload_id IS NOT NULL
          AND (status NOT IN ('uploading', 'verifying_remote')
               OR multipart_started_at < now() - INTERVAL '24 hours')
    """)
    for c in candidates:
        try_abort_multipart(get_storage(c), build_key(c), c.multipart_upload_id)

    # Bucket 级 lifecycle 兜底
    # （在 setup 时配 LIFECYCLE: 7 天 abort all incomplete multiparts）
```

### 3.5 RPO / RTO 量化

- **RPO ≤ 15 分钟**：PG WAL archive 频率
- **RTO ≤ 10 分钟**：standby promotion + recovery_routine 完成
- 部分 subtask 的进度可能丢失（最多 200ms 心跳合并窗口的字节），但已 verified 的 subtask 永不丢
- 详见 05 §5 备份与灾难恢复

---

## 4. 调度竞态与防御

### 4.1 调度循环

```python
# Controller 主调度循环
while running:
    eligible_executors = list_healthy_executors_with_capacity()

    for executor in eligible_executors:
        if executor.has_pending_response_in_buffer:
            continue  # 别覆盖未发送的 assignment

        # 在 SCHEDULER_LOCK 内调用 assign_subtask
        with SCHEDULER_LOCK:
            assignment = assign_subtask(executor.id, executor.epoch)

        if assignment:
            buffer_response(executor.id, assignment)

    sleep(scheduler_interval_ms)
```

### 4.2 SKIP LOCKED 选 candidate

```sql
-- 选下一个 candidate（避免与其他调度循环冲突）
SELECT id FROM file_subtasks
WHERE status = 'pending'
  AND (storage_region_match OR storage_region IS NULL)  -- 区域亲和
  AND task_id IN (SELECT id FROM download_tasks
                  WHERE status IN ('pending', 'scheduling', 'downloading')
                  ORDER BY priority DESC, created_at ASC)
ORDER BY priority DESC, created_at ASC
LIMIT 1
FOR UPDATE SKIP LOCKED;
```

`SKIP LOCKED` 让多调度器实例（active/standby）和单实例多线程都能安全运行。

### 4.3 优先级反转防护

如果高优先级任务到达时所有 executor 都被低优先级占满：

```python
def check_preemption(new_task: DownloadTask):
    if new_task.priority < 3:
        return  # 仅 priority=3 (urgent) 触发抢占

    busy_executors = get_busy_executors_with_lower_priority_subtasks(new_task.priority)
    if not busy_executors:
        return

    # 选 1 个最空闲的 executor 抢占
    target = pick_min_progress(busy_executors)

    # 抢占 = 把当前 subtask 状态退回 pending（保留进度，下次接着下）
    db.execute("""
        UPDATE file_subtasks
        SET status = 'pending', executor_id = NULL, executor_epoch = NULL,
            assignment_token = NULL
        WHERE executor_id = %s AND status IN ('assigned', 'downloading')
    """, (target.id,))
```

详见 06 §9 SLA 分级（v2.1 才落地完整抢占）。

---

## 5. 节点状态机的死循环修复

> 解决 D3。

### 5.1 问题回顾

v1.4 中 degraded ↔ suspect 之间存在状态泵：

```
degraded → 任务失败 1 次 → consecutive_failures++（但 status 不变）
        → 心跳超时 → suspect
        → 心跳恢复 → degraded （consecutive_failures 不重置）
        → 任务失败 1 次 → ... 永远在 degraded↔suspect 间跳，永远到不了 faulty
```

### 5.2 修复方案

引入两个独立计数器：

```sql
ALTER TABLE executors ADD COLUMN consecutive_heartbeat_failures INT NOT NULL DEFAULT 0;
ALTER TABLE executors ADD COLUMN consecutive_task_failures INT NOT NULL DEFAULT 0;
ALTER TABLE executors ADD COLUMN degraded_failure_streak INT NOT NULL DEFAULT 0;
```

| 计数器 | 增长触发 | 重置触发 |
|-------|---------|---------|
| `consecutive_heartbeat_failures` | 心跳超时 | 心跳恢复 |
| `consecutive_task_failures` | subtask 失败 | subtask 成功 |
| `degraded_failure_streak` | degraded 状态下任务失败 | 进入 healthy 时清零 |

### 5.3 Transition 规则

```python
def on_heartbeat_received(eid):
    e = get_executor(eid)
    e.last_heartbeat_at = now()
    e.consecutive_heartbeat_failures = 0
    if e.status == 'suspect':
        e.status = 'degraded'           # 不直接回 healthy，先观察
    elif e.status == 'faulty':
        e.status = 'probationary'       # 探测期，仅小任务

def on_heartbeat_timeout(eid):
    e = get_executor(eid)
    e.consecutive_heartbeat_failures += 1
    if e.consecutive_heartbeat_failures >= 3 and e.status == 'healthy':
        e.status = 'suspect'
    elif e.consecutive_heartbeat_failures >= 6 and e.status == 'suspect':
        e.status = 'faulty'
        reclaim_subtasks(eid, e.epoch)

def on_task_success(eid):
    e = get_executor(eid)
    e.consecutive_task_failures = 0
    if e.status == 'probationary':
        # canary 通过 N=3 次升级
        e.canary_passes += 1
        if e.canary_passes >= 3:
            e.status = 'healthy'
            e.degraded_failure_streak = 0  # 重置
            e.canary_passes = 0
    elif e.status == 'degraded':
        # 小任务连续 N=5 次成功
        e.degraded_recoveries += 1
        if e.degraded_recoveries >= 5:
            e.status = 'healthy'
            e.degraded_failure_streak = 0

def on_task_failure(eid):
    e = get_executor(eid)
    e.consecutive_task_failures += 1
    if e.status == 'healthy':
        if e.consecutive_task_failures >= 3:
            e.status = 'degraded'
            e.degraded_failure_streak = 0
    elif e.status == 'degraded':
        e.degraded_failure_streak += 1
        if e.degraded_failure_streak >= 10:
            e.status = 'faulty'
            reclaim_subtasks(eid, e.epoch)
    elif e.status == 'probationary':
        e.canary_passes = 0
        if e.canary_failures >= 2:
            e.status = 'faulty'
```

🔒 **不变量**：每次 transition 调用 `record_status_history(eid, from, to, reason, ts)`，CI 断言所有 transition 都有 history 记录。

### 5.4 Probationary 的 canary

`probationary` 状态只接受 1 个小任务（&lt;100MB），通过 N=3 次后才 healthy。
canary 任务从专门的 small-file 池子选；如果当前没有合适小任务，等 1 分钟后随机选个小 subtask。

---

## 6. 校验链路

> 解决 D5。

### 6.1 SHA256 计算的两种模式

**模式 A：单线程流式 SHA256（推荐用于中小文件）**

```python
async def download_streaming_sha(url, target_path, expected_sha):
    h = hashlib.sha256()
    async with target_path.open("wb") as f:
        async for chunk in stream_get(url):
            f.write(chunk)
            h.update(chunk)
    if h.hexdigest() != expected_sha:
        raise ChecksumMismatch()
```

✅ 单 IO 通道、单 CPU、内存 O(1)
❌ 单线程下载，不利用多 NIC 队列

**模式 B：多线程 + 完整文件二次扫描（用于 ≥1GB 文件）**

```python
# 阶段 1: N 线程并发 seek+write
async with asyncio.TaskGroup() as tg:
    for chunk_idx in range(n_chunks):
        tg.create_task(download_chunk_to_offset(url, target_path, chunk_idx))

# 阶段 2: 单线程顺序 SHA256
h = hashlib.sha256()
async with target_path.open("rb") as f:
    while chunk := await f.read(64 * 1024 * 1024):
        h.update(chunk)
if h.hexdigest() != expected_sha:
    raise ChecksumMismatch()
```

✅ 下载并行度高
❌ 二次扫描成本（17GB 文件读一次约 30s SSD）

📝 **决策**：v2.0 用模式 A 处理 &lt;1GB；模式 B 处理 ≥1GB。可配置阈值。
v2.2 引入 BLAKE3 后可改为"并发 hash 树"，消除二次扫描——但前提是 HF 上游也提供 BLAKE3。

### 6.2 多源 chunk-level 必须用模式 B

来自不同源的 chunk 写入同一文件，无法保证顺序，必须用模式 B 二次扫描。详见 06 §1.6。

### 6.3 远端校验（S3 ChecksumSHA256）

S3 multipart upload 时设 `ChecksumAlgorithm=SHA256`，每个 part 计算 sha，CompleteMultipartUpload 时 S3 自动验证整体并返回 `ChecksumSHA256`：

```python
upload_id = s3.create_multipart_upload(
    Bucket=b, Key=k, ChecksumAlgorithm='SHA256'
)['UploadId']
parts = []
for i, chunk in enumerate(chunks):
    resp = s3.upload_part(Bucket=b, Key=k, UploadId=upload_id, PartNumber=i+1,
                          Body=chunk, ChecksumSHA256=base64(sha256(chunk).digest()))
    parts.append({'PartNumber': i+1, 'ETag': resp['ETag'],
                  'ChecksumSHA256': resp['ChecksumSHA256']})
s3.complete_multipart_upload(Bucket=b, Key=k, UploadId=upload_id,
                              MultipartUpload={'Parts': parts})

# Verify with HEAD
head = s3.head_object(Bucket=b, Key=k, ChecksumMode='ENABLED')
remote_sha = base64.b64decode(head['ChecksumSHA256']).hex()
assert remote_sha == expected_sha
```

🔒 **不变量 5（再强调）**：任务级最终校验时，对**所有** subtask 比对 `expected_sha256 == actual_sha256`，不能只看 size。

### 6.4 跨源 SHA256 真值

详见 06 §1：HF 是真值来源（不变量 11）。其他源下载完成后用 HF 的 sha 验证。

---

## 7. 任务取消的 cancelling 中间态

> 解决 D8。

### 7.1 状态语义

`cancelling` 状态明确：

- task 进入 cancelling 后，scheduler 不再为其分配新 subtask
- in-flight subtask 被通知"软取消"：完成本 chunk 后停下，状态进 `cancelled`
- 已 `verified` 的 subtask **保留文件**（用户重启任务可复用）
- 全部 subtask 进入终态后，task 进入 `cancelled`

### 7.2 取消与完成的竞争

```
T1: 用户调 POST /cancel
T2: controller 设 task.status = 'cancelling'
T3: 同一时刻，subtask S 完成（competion API 在传输中）
T4: controller 收到 S 的 complete API
```

**T4 的处置**：

```python
def complete_subtask(s_id, ...):
    with db.transaction():
        s = db.get_for_update(s_id)
        task = db.get_for_update(s.task_id)

        if task.status == 'cancelling':
            # task 正在取消；s 完成的成果保留（标 verified）
            # 但不影响 task 走向 cancelled
            s.status = 'verified'
            db.commit()
            return {"status": "completed_during_cancel"}

        if task.status in ('cancelled', 'failed'):
            # task 已终态；s 的文件应清理
            schedule_cleanup(s.remote_storage_uri)
            s.status = 'cancelled'
            return {"status": "discarded"}

        # 正常路径
        s.status = 'verified'
```

🔒 **不变量**：`verified` subtask 在 task 进入 `cancelled` 后**保留文件**。这是与 v1.x 的关键差异，让用户的"取消后重启"零成本。

---

## 8. HF / 源全局降级

> 解决 D13 + OPS-08。

### 8.1 全局 throttle 状态机

```sql
CREATE TABLE source_throttle_state (
    source_id     VARCHAR(32) PRIMARY KEY,
    state         VARCHAR(16) NOT NULL,           -- normal / throttled / circuit_open
    rate_429_5m   FLOAT NOT NULL DEFAULT 0,        -- 滚动窗口
    rate_5xx_5m   FLOAT NOT NULL DEFAULT 0,
    speed_limit_global FLOAT,                      -- bytes/sec，0 表示无限
    last_updated  TIMESTAMPTZ NOT NULL DEFAULT now(),
    next_review_at TIMESTAMPTZ NOT NULL
);
```

**状态机**：

```
normal ──── 5min 内 429 率 > 5% ────► throttled (limit=50%)
normal ──── 5min 内 5xx 率 > 10% ───► circuit_open (limit=0)
throttled ── 持续 10min 缓解 ──────► normal
throttled ── 5xx 率 > 10% ─────────► circuit_open
circuit_open ── 5min 后试探 ───────► throttled (limit=10%)
circuit_open ── 试探仍失败 ────────► circuit_open (++ exp backoff)
```

### 8.2 控制器协调下发

每次心跳响应中带 `policies_to_apply.global_speed_limit_bytes_per_sec`，executor 必须遵守。
心跳间隔内速率超出 → executor 自降 + 上报警告。

### 8.3 任务进入 paused_external 而非 failed

```python
def on_subtask_failure(s, error):
    if error.is_external_throttle():
        s.status = 'paused_external'
        s.last_error = str(error)
        # 不计 retry_count；不视为 executor 故障
    else:
        s.status = 'failed_permanent' if not error.retryable else 'pending'
        s.retry_count += 1
```

`paused_external` 子任务由后台 sweeper 每 5min 检查源恢复，恢复后回 `pending`。

### 8.4 持久化（解决 D13）

`source_throttle_state` 写 PG，控制器重启后立即加载，不会"重启即全速"。

---

## 9. 磁盘满与 paused_disk_full

> 解决 D7。

### 9.1 双层保护

**Layer 1：分配前预检 + 预占**

```python
def can_assign_to_executor(s: FileSubTask, e: Executor) -> bool:
    reserved = sum_of_running_subtasks_bytes_remaining(e)
    if e.disk_free_gb * GB - reserved < s.expected_size + SAFETY_MARGIN:
        return False
    return True
```

**Layer 2：写时 ENOSPC 捕获**

```python
async def write_chunk(f, data):
    try:
        await f.write(data)
    except OSError as ex:
        if ex.errno == errno.ENOSPC:
            raise DiskFullError(executor_id=ME)
        raise
```

### 9.2 paused_disk_full 处置

捕获 `DiskFullError`：

1. 当前 subtask 状态 → `paused_disk_full`
2. Executor 立即上报心跳，`disk_free_gb=0`
3. Controller 标该 executor `degraded`，不再分配新任务
4. 已开始的其他 subtask 在该 executor 上完成（完成后释放 .parts/）
5. Executor 上报 disk 恢复（运维清理或扩容）→ paused_disk_full subtask 回 pending
6. 重新分配（可能给其他 executor，也可能仍是本 executor）

🔒 **不变量**：`paused_disk_full` 不计入 retry_count，不触发任务级 failure。

---

## 10. CDN URL 失效 + Commit Pin

> 解决 D4。

### 10.1 问题

HF CDN 签名 URL 5-10min 过期。下载到一半 URL 过期 → 403。如果 N-02 仅"刷新 URL"，新 URL 的内容**可能**对应 HF 上 force-push 后的 commit，导致拼装出错。

### 10.2 协议级 commit pin

每个文件下载请求必须带 `If-Match: "{X-Repo-Commit}"`：

```python
def download_chunk(file, byte_range, expected_commit):
    headers = {
        "Range": f"bytes={byte_range[0]}-{byte_range[1]}",
        "If-Match": expected_commit,           # ETag 形式 "<commit-sha>"
    }
    resp = client.get(url, headers=headers)
    if resp.status_code == 412:                # Precondition Failed
        raise CommitChanged(expected=expected_commit, got=resp.headers.get("ETag"))
    if resp.status_code in (403, 410):
        raise UrlExpired()
    # 校验 response 中的 commit
    if resp.headers.get("X-Repo-Commit") != expected_commit:
        raise CommitChanged()
    yield from resp.iter_content()
```

### 10.3 失败处置

`CommitChanged` 被抛 → subtask `failed_permanent`（不可重试），任务级最终校验也失败。
用户必须用新的 revision sha 重建任务。

📝 **决策**：HF 实际不支持 `If-Match`，但响应 header 一定带 `X-Repo-Commit`。我们要求 client 校验 header 不变；这是软 pin。但因 v2.0 不变量是 `revision` 必须为 40-char sha 而非 `main`，HF 在 sha-pin 模式下不会发生 force-push 冲突。
软 pin 是为了防御中间代理 / mirror 推送了不一致的内容。

---

## 11. WebSocket 顺序与重连

> 解决 D12。

### 11.1 Snapshot + Delta + Seq 协议

详见 02 §5。关键不变量：

- `seq` 单调递增，全局 ordering（per-connection）
- client 检测到 gap → resync 拿 snapshot
- server 缓存最近 1 分钟的 delta；超过则强制 snapshot

### 11.2 控制器侧实现

```python
class WSChannel:
    def __init__(self):
        self.seq = 0
        self.delta_buffer = collections.deque(maxlen=5000)  # ~1min @ 100/s

    def push_delta(self, patches):
        self.seq += 1
        msg = {"type": "delta", "seq": self.seq, "patches": patches}
        self.delta_buffer.append(msg)
        broadcast(msg)

    def handle_resync(self, last_seq):
        if last_seq + 1 in [m["seq"] for m in self.delta_buffer]:
            # 续推
            for m in self.delta_buffer:
                if m["seq"] > last_seq:
                    send(m)
        else:
            # 太久 → 重发 snapshot
            send_snapshot()
```

---

## 12. 关键不变量自查清单（CI 断言）

> 这些是 v2.0 必须保持的属性。每条都应有 test 或 lint 校验。

```python
# tests/test_invariants.py

def test_no_double_assignment():
    """不变量：同一 subtask 不能同时分配给两个 executor"""
    # 用 SQL constraint 实际上已强制：UNIQUE(task_id, filename)
    # 这里测：CAS 失败时不会写 enqueue
    ...

def test_assignment_token_required_for_complete():
    """不变量 6"""
    response = post(f"/api/executors/X/subtasks/Y/complete",
                    headers={"X-Assignment-Token": "wrong-token"})
    assert response.status_code == 409

def test_recovery_three_way_check():
    """三联校验"""
    # 模拟：DB 中状态 verifying_remote，远端 size 不匹配
    db.set_subtask_status("S", "verifying_remote", multipart_upload_id="upload1")
    mock_storage.head_object.return_value = HeadResp(size=999, sha256="x")
    recovery_routine()
    assert db.get_subtask("S").status == "pending"

def test_cancelling_keeps_verified_files():
    """不变量：取消后 verified 文件保留"""
    task = create_task_with_subtasks(verified=10, in_flight=3)
    cancel(task.id)
    wait_for(lambda: get_task(task.id).status == "cancelled")
    for s in task.verified_subtasks:
        assert storage.exists(s.remote_storage_uri)

def test_paused_external_does_not_count_as_failure():
    """不变量"""
    s = create_subtask()
    fail_with_429(s)
    assert s.status == "paused_external"
    assert s.retry_count == 0

def test_global_throttle_persists_across_restart():
    """D13 修复"""
    set_global_throttle("huggingface", "circuit_open")
    restart_controller()
    assert get_global_throttle("huggingface") == "circuit_open"

def test_state_machine_no_illegal_transitions():
    """状态机健全性"""
    for state in TASK_TERMINAL_STATES:
        for next_state in ALL_STATES:
            with pytest.raises(IllegalTransition):
                transition(state, next_state)
```

---

## 13. 与其他文档的链接

- 数据模型：→ [01-architecture.md](./01-architecture.md) §4
- API 协议：→ [02-protocol.md](./02-protocol.md)
- 安全模型：→ [04-security-and-tenancy.md](./04-security-and-tenancy.md)
- SLO / 监控：→ [05-operations.md](./05-operations.md)
- 多源 SHA256 真值链：→ [06-platform-and-ecosystem.md](./06-platform-and-ecosystem.md) §1.13
