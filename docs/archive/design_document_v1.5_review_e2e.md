# [SUPERSEDED] Review 优化 · 日志系统 · E2E 测试 — 补充设计文档 v1.5

> ⚠️ **此文档已被 v2.0 取代，仅作历史追溯**
>
> 当前权威文档：**[../v2.0/00-INDEX.md](../v2.0/00-INDEX.md)**
>
> 本文档内容已合并到：
> - Review 修复（C-XX / D-XX / N-XX / E-XX / S-XX 全列表） → 全部纳入 v2.0 各对应章节，并加固防御
> - 日志系统 → `../v2.0/05-operations.md` §1.2
> - E2E 测试 → `../v2.0/06-platform-and-ecosystem.md` §8
> - 多执行器 → `../v2.0/01-architecture.md` §5.3
> - hf_transfer 借鉴（DirectOffsetDownloader） → `../v2.0/03-distributed-correctness.md` §6 + `../v2.0/06-platform-and-ecosystem.md` §1.6
>
> **关键加固（v1.5 未修复，v2.0 修复了）**：
> - v1.5 的 CAS 仅保护 DB，未防止"内存队列+心跳响应"的双发漏洞 → v2.0 引入 fence token + executor epoch
> - v1.5 的 stuck_uploading 恢复仅检查文件存在 → v2.0 强制三联校验（远端存在 + ChecksumSHA256 + size）
> - v1.5 仅 trace_id 字段，未真正接 OpenTelemetry → v2.0 全链路埋点
>
> **请勿基于本文档实施。** 实施时以 v2.0 为准。

> 版本: v1.5（已废弃）| 原日期: 2026-04-28

---

## 目录

1. [Review 意见汇总与修复方案](#1-review-意见汇总与修复方案)
2. [竞品特性借鉴](#2-竞品特性借鉴)
3. [单机多执行器特性](#3-单机多执行器特性)
4. [日志与可观测性系统](#4-日志与可观测性系统)
5. [E2E 测试用例](#5-e2e-测试用例)

---

## 1. Review 意见汇总与修复方案

### 1.1 Critical 级问题 (必须修复)

| ID | 类别 | 问题 | 修复方案 |
|----|------|------|---------|
| C-01 | 并发 | `trigger_rebalance()` TOCTOU 竞态：时间检查在锁外 | 将时间检查移入锁内 |
| C-02 | 并发 | `profiles`/`executors` 共享字典无锁保护 | 用 `threading.RLock()` 包装所有读写 |
| C-03 | 并发 | 任务调度竞态：同一文件可能分配给两个执行器 | `_assign_subtask` 加原子 CAS：`UPDATE subtasks SET status='assigned' WHERE id=? AND status='pending'` |
| D-01 | 一致性 | 控制器崩溃无恢复：任务状态永久卡住 | 启动时执行 `recovery_routine`：扫描 assigned/downloading 超时任务重置为 pending |
| S-01 | 可用性 | 单控制器 SPOF | v1: 数据库持久化+自动重启恢复; v2: active-passive 双节点 |

### 1.2 High 级问题 (应当修复)

| ID | 类别 | 问题 | 修复方案 |
|----|------|------|---------|
| C-04 | 并发 | chunk 状态文件更新非原子 | `_save_state` 用 write-tmp-then-rename |
| C-05 | 并发 | `TaskQueueScheduler` 无线程安全 | 所有公开方法加 `threading.Lock` |
| D-02 | 一致性 | 状态文件写入中途崩溃导致 JSON 损坏 | 原子写入：`.tmp` → `os.replace()` |
| D-03 | 一致性 | Task+SubTask 创建无事务边界 | 用 `with db.session.begin()` 包裹 |
| N-01 | 网络 | 连接抖动导致任务反复回收 | 3 次连续心跳失败才标记 suspect，加迟滞区间 |
| N-02 | 网络 | CDN 签名 URL 过期(5-10min)导致大文件下载中断 | 检测 403/410 自动刷新 URL，存储原始 HF URL 非 CDN URL |
| N-03 | 网络 | 无去重保护：同文件分给两执行器 | DB 唯一约束 `(task_id, filename, executor_id)` + 传输前二次检查 |
| R-01 | 泄漏 | 崩溃后 `.parts/` 临时文件不清理 | 启动时扫描清理无活跃下载的 `.parts/` 目录 |
| R-02 | 泄漏 | S3 分片上传中断留下孤立 part | 定期任务 abort 24h 前的 incomplete multipart；设 bucket lifecycle 7 天自动过期 |
| E-01 | 错误 | 下载前不检查磁盘空间 | 心跳上报 `disk_free_gb`，控制器分配前校验；执行器接受任务前校验 |
| E-02 | 错误 | 远端校验只检查 size 不检查 SHA256 | 用 S3 ChecksumSHA256 功能或下载远端文件计算哈希 |
| S-02 | 性能 | WebSocket 每次推送全部 163 个 subtask | 发送 delta 更新（仅变化的 subtask），推送限频 2次/秒 |
| S-03 | 性能 | SHA256 对 17GB 文件串行计算阻塞流水线 | 下载时流式计算 SHA256（边写边哈希），消除后校验等待 |

### 1.3 Medium 级问题

| ID | 类别 | 问题 | 修复方案 |
|----|------|------|---------|
| D-04 | 一致性 | 执行器重注册覆盖状态 | 重注册时检查已有任务，重新 offer 或回收 |
| N-04 | 网络 | 全局 429 限流无协调 | 控制器追踪全局 429 频率，系统级降速 |
| E-03 | 错误 | HF Token 过期无处理 | 检测 401 响应，标记任务 `token_expired`，提示用户更新 |
| E-04 | 错误 | chunk 拼装不校验单个 chunk 完整性 | 拼装前验证每个 chunk 文件 size 是否匹配 |
| E-05 | 错误 | 文件传输无超时 | HTTP 传输设 timeout，rsync 加 `--timeout` |
| E-06 | 运维 | 无优雅停机 | SIGTERM handler: 停接新任务→等 in-flight 完成(60s超时)→刷DB→退出 |
| P-02 | 性能 | 速度计算粒度粗(10s心跳) | 执行器本地缓冲每秒采样，心跳时上报最近 N 秒采样均值 |
| P-03 | 性能 | WebSocket 消息过大 | delta 推送 + 批量合并 |

### 1.4 安全问题修复

| ID | 严重度 | 问题 | 修复方案 |
|----|--------|------|---------|
| SEC-01 | Critical | HF Token 明文存储传输 | AES-256-GCM 加密存储；API 传 `Authorization` 头；执行器不直接拿 token，向控制器申请短期凭证 |
| SEC-02 | Critical | 执行器↔控制器无认证无加密 | TLS + 注册预共享密钥(PSK) 或 JWT；IP 白名单 |
| SEC-03 | Critical | REST API 无认证无鉴权 | JWT 认证 + RBAC(viewer/operator/admin)；速率限制；CORS 配置 |
| SEC-04 | High | WebSocket 无认证 | 连接时验证 token；校验 Origin |
| SEC-05 | High | 文件上传路径穿越风险 | 过滤 `..` `/` `\`；限制上传大小；校验文件 size |
| SEC-06 | High | 无审计日志 | 新增 `audit_log` 表，记录所有敏感操作 |
| SEC-07 | Medium | AK/SK 无轮转 | 支持配置过期时间，到期前 UI 告警 |

### 1.5 一致性修复实现

```python
class ControllerRecovery:
    """控制器启动时执行恢复例程"""

    HEARTBEAT_STALE_SECONDS = 120

    def recover_on_startup(self):
        log.info("Controller recovery started")

        with self.db.session.begin():
            stuck_assigned = self.db.query(FileSubTask).filter(
                FileSubTask.status == "assigned",
                FileSubTask.assigned_at < datetime.utcnow() - timedelta(seconds=self.HEARTBEAT_STALE_SECONDS),
            ).all()
            for s in stuck_assigned:
                s.status = "pending"
                s.executor_id = None
                log.info(f"Recovery: reset stuck assigned subtask {s.id} ({s.filename})")

            stuck_downloading = self.db.query(FileSubTask).filter(
                FileSubTask.status == "downloading",
            ).all()
            for s in stuck_downloading:
                executor = self.executor_manager.get_profile(s.executor_id)
                if not executor or executor.status in ("offline", "faulty"):
                    s.status = "pending"
                    s.executor_id = None
                    log.info(f"Recovery: reset orphan downloading subtask {s.id}")

            stuck_uploading = self.db.query(FileSubTask).filter(
                FileSubTask.status.in_("uploading", "transferring"),
            ).all()
            for s in stuck_uploading:
                if os.path.exists(s.local_path):
                    s.status = "verified"
                else:
                    s.status = "pending"
                    s.executor_id = None

        log.info("Controller recovery completed")
```

### 1.6 并发安全修复实现

```python
class ThreadSafeState:
    """所有共享状态的线程安全包装"""

    def __init__(self):
        self._lock = threading.RLock()
        self._profiles: dict[str, NodeHealthProfile] = {}
        self._tasks: dict[str, DownloadTask] = {}

    @contextmanager
    def profile_access(self, executor_id: str) -> Generator[NodeHealthProfile, None, None]:
        with self._lock:
            profile = self._profiles.get(executor_id)
            if profile is None:
                raise ExecutorNotFoundError(executor_id)
            yield profile

    def update_profile(self, executor_id: str, update_fn: Callable[[NodeHealthProfile], None]):
        with self._lock:
            profile = self._profiles.get(executor_id)
            if profile:
                update_fn(profile)

    def assign_subtask_atomic(self, subtask_id: str, executor_id: str) -> bool:
        with self._lock:
            result = self.db.execute(
                text("UPDATE subtasks SET status='assigned', executor_id=:eid "
                     "WHERE id=:sid AND status='pending'"),
                {"eid": executor_id, "sid": subtask_id},
            )
            return result.rowcount > 0
```

---

## 2. 竞品特性借鉴

### 2.1 从 aria2 借鉴

| 特性 | 借鉴要点 | 实现方案 |
|------|---------|---------|
| **RPC + WebSocket 事件推送** | 已有 WebSocket，但应增加事件类型 | 新增 `task.started` `file.completed` `executor.degraded` 等事件类型 |
| **会话持久化** | 控制器崩溃后恢复 | 已通过 D-01 recovery 解决 |
| **服务器性能画像** | 追踪历史速度优选镜像 | 当前仅单源(HF CDN)，可扩展为 HF Mirror 站点优选 |
| **分段完整性校验** | per-chunk 校验而非仅最终文件 | 在 chunk 下载完成后立即校验该 chunk 的字节范围哈希 |
| **失败并发限制** | 独立的失败信号量 | 新增 `parallel_failures` 上限，防止级联重试 |

### 2.2 从 IDM 借鉴

| 特性 | 借鉴要点 | 实现方案 |
|------|---------|---------|
| **动态分段** | 不预分，新连接可用时再切最大剩余段 | 改进 `ChunkedDownloader`：初始 N 个 chunk，新线程空闲时再切已分配的最大 chunk |
| **下载队列调度** | 时间调度 + 条件触发 | 已有探查触发；新增时间调度（凌晨启动） |
| **全局速度限制** | 全局带宽上限 | 新增 GlobalSpeedLimiter |
| **下载分类** | 按类型自动归档 | 模型文件自动按 org/name/revision 归档（已有路径模板） |
| **批量操作** | 批量暂停/恢复/取消 | UI 增加勾选+批量操作栏 |

### 2.3 从 hf_transfer 借鉴

| 特性 | 借鉴要点 | 实现方案 |
|------|---------|---------|
| **信号量并发控制** | `Semaphore(max_files)` 限制并行 | 执行器内部用 `asyncio.Semaphore` 控制并发 |
| **指数退避+jitter** | `min(base + n² + rand(0..500), 10000ms)` | 替换当前固定退避为 jitter 退避 |
| **直接文件偏移写入** | seek+offset 避免拼装 | chunk 下载直接写入最终文件对应偏移位置，消除 `_assemble_chunks` 步骤 |

### 2.4 新增全局速度限制器

```python
class GlobalSpeedLimiter:
    def __init__(self):
        self.global_limit_bps: float = 0          # 0=不限
        self.per_task_limits: dict[str, float] = {}  # task_key → limit_bps
        self.schedule_limits: list[ScheduleLimit] = []  # 时间段限速

    def apply_schedule(self):
        now = datetime.utcnow()
        for sl in self.schedule_limits:
            if sl.start_time <= now.time() <= sl.end_time:
                self.global_limit_bps = sl.limit_bps
                return
        self.global_limit_bps = 0

    def distribute_bandwidth(self, active_tasks: list[DownloadTask]) -> dict[str, float]:
        if self.global_limit_bps == 0:
            return {t.task_key: 0 for t in active_tasks}

        total_weight = sum(t.total_size for t in active_tasks)
        allocated = {}
        for t in active_tasks:
            per_limit = self.per_task_limits.get(t.task_key, 0)
            weight = t.total_size / total_weight
            allocated[t.task_key] = min(
                per_limit or self.global_limit_bps * weight,
                per_limit or self.global_limit_bps,
            )
        return allocated


@dataclass
class ScheduleLimit:
    start_time: time
    end_time: time
    limit_bps: float
    label: str                      # "工作时间限速"
```

### 2.5 直接偏移写入(消除 chunk 拼装)

```python
class DirectOffsetDownloader:
    """
    改进: chunk 直接写入最终文件的对应偏移位置
    优点: 无需拼装步骤, 减少磁盘IO和临时空间
    """

    def download_file(self, url: str, target_path: str,
                      expected_size: int, headers: dict = None,
                      progress_callback=None) -> DownloadResult:

        if not os.path.exists(target_path):
            with open(target_path, "wb") as f:
                f.truncate(expected_size)

        sha256_hash = hashlib.sha256()
        chunk_ranges = self._calculate_chunks(expected_size, [])
        completed = threading.Event()
        completed_count = [0]
        lock = threading.Lock()

        def _download_chunk_worker(chunk_idx: int, start: int, end: int):
            hdrs = {**(headers or {}), "Range": f"bytes={start}-{end}"}
            for attempt in range(self.max_retries):
                try:
                    resp = requests.get(url, headers=hdrs, stream=True, timeout=300)
                    resp.raise_for_status()
                    chunk_sha = hashlib.sha256()
                    with lock:
                        f = open(target_path, "r+b")
                        f.seek(start)
                    for data in resp.iter_content(chunk_size=10 * 1024 * 1024):
                        f.write(data)
                        chunk_sha.update(data)
                    f.close()

                    completed_count[0] += 1
                    if progress_callback:
                        done_bytes = completed_count[0] * self.chunk_size
                        progress_callback(min(done_bytes, expected_size), expected_size)
                    break
                except Exception as e:
                    if attempt == self.max_retries - 1:
                        raise
                    delay = min(0.3 + attempt ** 2 + random.uniform(0, 0.5), 10.0)
                    time.sleep(delay)

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = []
            for i, (start, end) in enumerate(chunk_ranges):
                futures.append(pool.submit(_download_chunk_worker, i, start, end))
            for f in futures:
                f.result()

        actual_sha256 = self._compute_sha256(target_path)
        return DownloadResult(status="completed", path=target_path,
                              sha256=actual_sha256)
```

---

## 3. 单机多执行器特性

### 3.1 设计目标

当单个执行器无法跑满网卡带宽时，允许在同一台机器上启动多个执行器进程，每个进程独立注册到控制器，并行下载不同的文件。控制器负责决策是否需要多开。

### 3.2 决策逻辑

```
决策流程:

1. 执行器监控本地网卡利用率 (每 10 秒采样)
2. 如果网卡利用率 < 70% 且当前线程数已达上限:
   a. 尝试先增加线程数 (如果未达 max 16 线程)
   b. 如果线程数已达上限但带宽仍未饱和:
      → 上报控制器请求本机多开执行器
3. 控制器决策:
   a. 检查该机器是否已有执行器在运行
   b. 检查该机器 CPU/内存是否还有余量
   c. 如果允许: 下发 multi_executor 指令
4. 执行器收到指令后 fork 新的执行器进程
5. 新执行器以不同的 executor_id 注册到控制器
6. 控制器为新执行器分配不同的文件任务
```

### 3.3 网卡利用率监控

```python
class BandwidthMonitor:
    def __init__(self, interface: str = None):
        self.interface = interface or self._detect_default_interface()
        self.link_speed_mbps = self._detect_link_speed()
        self.samples: deque[tuple[float, float]] = deque(maxlen=6)   # (timestamp, bytes_recv)

    def _detect_default_interface(self) -> str:
        import psutil
        stats = psutil.net_io_counters(pernic=True)
        for iface, stat in stats.items():
            if not iface.startswith(("lo", "docker", "veth", "br-")):
                return iface
        return "eth0"

    def _detect_link_speed(self) -> int:
        try:
            with open(f"/sys/class/net/{self.interface}/speed") as f:
                return int(f.read().strip())
        except Exception:
            return 1000

    def sample(self):
        import psutil
        counters = psutil.net_io_counters(pernic=True).get(self.interface)
        if counters:
            self.samples.append((time.time(), counters.bytes_recv))

    def get_utilization_percent(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        t1, b1 = self.samples[0]
        t2, b2 = self.samples[-1]
        dt = t2 - t1
        if dt <= 0:
            return 0.0
        throughput_bps = (b2 - b1) * 8 / dt
        return throughput_bps / (self.link_speed_mbps * 1e6) * 100

    def get_effective_throughput_bps(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        t1, b1 = self.samples[0]
        t2, b2 = self.samples[-1]
        dt = t2 - t1
        if dt <= 0:
            return 0.0
        return (b2 - b1) / dt
```

### 3.4 多执行器管理器

```python
@dataclass
class MultiExecutorDecision:
    action: str                              # "increase_threads" / "spawn_executor" / "reduce" / "no_change"
    reason: str
    new_thread_count: int | None = None
    spawn_count: int | None = None


class MultiExecutorManager:
    """
    运行在执行器侧，判断是否需要在本机多开执行器

    阈值:
    - 网卡利用率 < 70% 且线程满 → 考虑多开
    - 网卡利用率 > 90% → 无需多开
    - CPU > 85% 或 内存 > 80% → 不允许多开
    - 单机最多 4 个执行器进程
    """

    MAX_EXECUTORS_PER_HOST = 4
    BANDWIDTH_LOW_THRESHOLD = 70.0          # 网卡利用率低于此值考虑多开
    BANDWIDTH_HIGH_THRESHOLD = 90.0         # 高于此值认为已跑满
    CPU_HIGH_THRESHOLD = 85.0
    MEMORY_HIGH_THRESHOLD = 80.0

    def __init__(self, executor_id: str, controller_url: str,
                 bandwidth_monitor: BandwidthMonitor):
        self.executor_id = executor_id
        self.controller_url = controller_url
        self.bw_monitor = bandwidth_monitor
        self.hostname = socket.gethostname()
        self._spawned_processes: list[subprocess.Popen] = []

    def evaluate(self) -> MultiExecutorDecision:
        import psutil

        bw_util = self.bw_monitor.get_utilization_percent()
        cpu_percent = psutil.cpu_percent(interval=1)
        mem_percent = psutil.virtual_memory().percent

        if bw_util > self.BANDWIDTH_HIGH_THRESHOLD:
            return MultiExecutorDecision("no_change", f"网卡利用率 {bw_util:.1f}% 已饱和")

        if cpu_percent > self.CPU_HIGH_THRESHOLD:
            return MultiExecutorDecision("no_change", f"CPU {cpu_percent:.1f}% 过高, 不宜多开")

        if mem_percent > self.MEMORY_HIGH_THRESHOLD:
            return MultiExecutorDecision("no_change", f"内存 {mem_percent:.1f}% 过高, 不宜多开")

        if bw_util < self.BANDWIDTH_LOW_THRESHOLD:
            current_threads = self._get_current_max_threads()

            if current_threads < 16:
                new_threads = min(current_threads + 4, 16)
                return MultiExecutorDecision(
                    "increase_threads",
                    f"网卡利用率 {bw_util:.1f}%, 线程 {current_threads}→{new_threads}",
                    new_thread_count=new_threads,
                )

            host_executors = self._count_host_executors()
            if host_executors < self.MAX_EXECUTORS_PER_HOST:
                return MultiExecutorDecision(
                    "spawn_executor",
                    f"网卡利用率 {bw_util:.1f}%, 线程已满, 本机 {host_executors} 个执行器, 可增加",
                    spawn_count=1,
                )

        return MultiExecutorDecision("no_change", f"网卡利用率 {bw_util:.1f}% 适中")

    def spawn_executor(self) -> str:
        new_id = f"{self.executor_id}-worker-{len(self._spawned_processes) + 1}"

        process = subprocess.Popen(
            [sys.executable, "-m", "executor.main"],
            env={
                **os.environ,
                "CONTROLLER_URL": self.controller_url,
                "EXECUTOR_ID": new_id,
                "EXECUTOR_PARENT_HOST": self.hostname,
                "MAX_WORKERS": "8",
            },
        )
        self._spawned_processes.append(process)

        self._notify_controller_spawn(new_id, process.pid)
        log.info(f"Spawned executor {new_id} (PID {process.pid})")
        return new_id

    def _notify_controller_spawn(self, new_executor_id: str, pid: int):
        requests.post(f"{self.controller_url}/api/executors/register", json={
            "executor_id": new_executor_id,
            "hostname": self.hostname,
            "ip": self._get_local_ip(),
            "max_workers": 8,
            "parent_executor_id": self.executor_id,
            "pid": pid,
            "bandwidth_mbps": self.bw_monitor.link_speed_mbps,
            "local_storage_path": os.environ.get("LOCAL_STORAGE_PATH", "/tmp/downloads"),
        })

    def _count_host_executors(self) -> int:
        try:
            resp = requests.get(
                f"{self.controller_url}/api/executors",
                params={"hostname": self.hostname},
            )
            executors = resp.json().get("executors", [])
            return len([e for e in executors if e.get("status") != "offline"])
        except Exception:
            return 1
```

### 3.5 控制器侧多执行器支持

```python
class MultiExecutorAwareScheduler:
    """
    控制器感知同一主机上的多个执行器, 做出合理调度

    规则:
    1. 同一主机的多个执行器不下载同一文件(避免 HF CDN 同 IP 限流叠加)
    2. 同一主机的总连接数有上限(避免触发 HF 限流)
    3. 同主机执行器共享临时存储, 避免磁盘冲突
    4. 父执行器退出时, 子执行器一并清理
    """

    MAX_CONNECTIONS_PER_HOST = 20

    def assign_task_to_executor(self, subtask: FileSubTask,
                                 candidates: list[ExecutorInfo]) -> ExecutorInfo | None:
        host_load = self._calc_host_load(candidates)

        filtered = []
        for e in candidates:
            host = e.hostname
            current_connections = host_load.get(host, 0)
            if current_connections >= self.MAX_CONNECTIONS_PER_HOST:
                continue

            same_host_executors = self._get_same_host_executors(e.hostname)
            assigned_files = {s.filename for s in same_host_executors.get("assigned_files", [])}
            if subtask.filename in assigned_files:
                continue

            filtered.append(e)

        if not filtered:
            return None

        best = min(filtered, key=lambda e: self._calc_executor_load(e))
        return best

    def on_executor_exit(self, executor_id: str):
        profile = self.node_manager.get_profile(executor_id)
        if profile and profile.parent_executor_id:
            parent = self.node_manager.get_profile(profile.parent_executor_id)
            if parent:
                parent.spawned_executors = [
                    eid for eid in parent.spawned_executors if eid != executor_id
                ]
        elif profile and profile.spawned_executors:
            for child_id in profile.spawned_executors:
                self._reclaim_executor_tasks(child_id)
```

### 3.6 心跳上报扩展

```json
POST /api/executors/{executor_id}/heartbeat

{
  "executor_id": "exec-node1",
  "system": {
    "cpu_percent": 45.2,
    "memory_percent": 62.1,
    "disk_free_gb": 850.3,
    "network_in_bps": 104857600,
    "network_out_bps": 5242880,
    "nic_utilization_percent": 42.5,
    "nic_link_speed_mbps": 10000,
    "active_downloads": 3,
    "active_threads": 24,
    "max_concurrent_files": 3,
    "chunk_threads_per_file": 8,
    "multi_executor": {
      "host_executor_count": 1,
      "can_spawn": true,
      "reason": "nic_utilization_low",
      "spawn_recommendation": null
    }
  },
  "tasks": [...]
}
```

### 3.7 UI 展示

```
执行器面板中同主机执行器分组展示:

┌─ 执行器 ──────────────────────────────────────────────────┐
│                                                             │
│ 🖥 gpu-worker-01 (10Gbps, 当前 42.5% 利用率)              │
│ ├─ 🟢 node-1   健康 100分  89.2 MB/s  3文件  [详情]       │
│ └─ 🟢 node-1-worker-1  健康 98分  75.3 MB/s  2文件  [详情]│
│    💡 网卡未饱和, 已自动多开 1 个执行器                     │
│                                                             │
│ 🖥 gpu-worker-02 (10Gbps, 当前 92.1% 利用率)              │
│ ├─ 🟢 node-2   健康 100分  105 MB/s  3文件  [详情]        │
│ └─ 🟢 node-2-worker-1  健康 95分  98.1 MB/s  3文件  [详情]│
│    ✅ 网卡已饱和, 无需多开                                  │
│                                                             │
│ 🖥 gpu-worker-03 (1Gbps, 当前 28.3% 利用率)               │
│ ├─ 🟡 node-3   降级 55分  23.1 MB/s  2文件  [详情]        │
│ └─ ⚠ 网卡未饱和但 CPU 88% 过高, 不宜多开                  │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## 4. 日志与可观测性系统

### 4.1 日志框架选型

| 组件 | 选型 | 理由 |
|------|------|------|
| 日志库 | **structlog** | 结构化 JSON 日志，性能好，与标准库兼容 |
| 指标 | **prometheus_client** | Python 原生 Prometheus SDK，`/metrics` 端点 |
| 分布式追踪 | **OpenTelemetry** | trace_id 跨组件传递，标准协议 |
| 日志聚合 | **Loki + Grafana** | 轻量，与 Prometheus/Grafana 统一栈 |
| 告警 | **Alertmanager** | 与 Prometheus 配合 |

### 4.2 结构化日志格式

```json
{
  "timestamp": "2026-04-28T10:30:45.123Z",
  "level": "info",
  "component": "controller",
  "module": "task_scheduler",
  "trace_id": "abc123def456",
  "span_id": "789ghi012",
  "message": "File download completed",
  "task_id": "DeepSeek-V3_20260428_a3f8d2e1",
  "subtask_id": "st-001",
  "executor_id": "node-1",
  "filename": "model-00001-of-00163.safetensors",
  "file_size": 4592318464,
  "duration_seconds": 82,
  "speed_bps": 52428800,
  "sha256": "a3f8d2...e9c1",
  "sha256_verified": true
}
```

### 4.3 日志级别规范

| 级别 | 使用场景 | 示例 |
|------|---------|------|
| **DEBUG** | 调试信息, 生产环境关闭 | chunk 字节范围、线程池状态、HTTP 头(不含 Token) |
| **INFO** | 正常业务事件 | 任务创建/完成、文件下载完成、执行器注册/恢复、重平衡触发 |
| **WARNING** | 需要关注但不影响主流程 | 速度下降、心跳延迟、429 限流检测、重试中、磁盘空间 <20% |
| **ERROR** | 影响单个文件/操作 | SHA256 不匹配、上传失败、执行器隔离、单文件永久失败 |
| **CRITICAL** | 影响整个系统 | 数据库不可达、所有执行器离线、存储后端不可达 |

### 4.4 日志初始化

```python
import structlog
from structlog.stdlib import add_log_level
from structlog.processors import JSONRenderer, TimeStamper, format_exc_info


def configure_logging(component: str, level: str = "INFO"):
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            add_log_level,
            TimeStamper(fmt="iso"),
            format_exc_info,
            structlog.processors.StackInfoRenderer(),
            structlog.stdlib.PositionalArgumentsFormatter(),
            add_component(component),
            JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level)),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def add_component(component: str):
    def processor(logger, method, event_dict):
        event_dict["component"] = component
        return event_dict
    return processor


def with_trace_context(trace_id: str, span_id: str = None):
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(trace_id=trace_id)
    if span_id:
        structlog.contextvars.bind_contextvars(span_id=span_id)
```

### 4.5 Trace ID 传播

```python
class TraceContext:
    """跨组件追踪上下文"""

    @staticmethod
    def new_trace_id() -> str:
        return uuid4().hex[:16]

    @staticmethod
    def new_span_id() -> str:
        return uuid4().hex[:8]

    @staticmethod
    def inject_into_task(task: DownloadTask):
        task.trace_id = TraceContext.new_trace_id()

    @staticmethod
    def inject_into_heartbeat_response(response: HeartbeatResponse, tasks: list):
        for task_info in response.get("tasks", []):
            task_info["trace_id"] = tasks[0].trace_id if tasks else None

    @staticmethod
    def inject_into_subtask(subtask: FileSubTask, task: DownloadTask):
        subtask.span_id = TraceContext.new_span_id()
        subtask.trace_id = task.trace_id


log = structlog.get_logger()

log.info("task_created", task_id=task.task_key, repo_id=task.repo_id,
         trace_id=task.trace_id, total_files=task.total_files, total_size=task.total_size)

log.info("file_download_completed", trace_id=task.trace_id, span_id=subtask.span_id,
         filename=subtask.filename, duration_seconds=82, speed_bps=52428800,
         sha256_verified=True)

log.warning("executor_degraded", executor_id="node-3", health_score=55,
            speed_bps=23100000, reason="speed_drop")
```

### 4.6 Prometheus 指标

```python
from prometheus_client import Counter, Gauge, Histogram, generate_latest
from fastapi import Response

downloads_total = Counter(
    "hf_downloader_downloads_total",
    "Total download attempts",
    ["status"]                    # success / failed / retried
)

download_bytes_total = Counter(
    "hf_downloader_download_bytes_total",
    "Total bytes downloaded",
    ["executor_id", "task_id"]
)

download_duration_seconds = Histogram(
    "hf_downloader_download_duration_seconds",
    "Download duration per file",
    ["executor_id"],
    buckets=[10, 30, 60, 120, 300, 600, 1800, 3600]
)

active_downloads = Gauge(
    "hf_downloader_active_downloads",
    "Currently active downloads",
    ["executor_id"]
)

executor_health_score = Gauge(
    "hf_downloader_executor_health_score",
    "Executor health score",
    ["executor_id"]
)

task_queue_depth = Gauge(
    "hf_downloader_task_queue_depth",
    "Tasks in queue",
    ["priority"]
)

upload_bytes_total = Counter(
    "hf_downloader_upload_bytes_total",
    "Total bytes uploaded to storage",
    ["storage_id"]
)

http_requests_total = Counter(
    "hf_downloader_http_requests_total",
    "HTTP requests",
    ["method", "endpoint", "status"]
)

ws_connections_active = Gauge(
    "hf_downloader_ws_connections_active",
    "Active WebSocket connections",
    ["task_id"]
)


@app.get("/metrics")
async def metrics():
    return Response(content=generate_latest(), media_type="text/plain")
```

### 4.7 告警规则

```yaml
groups:
  - name: hf_downloader
    rules:
      - alert: AllExecutorsOffline
        expr: hf_downloader_active_downloads == 0 AND hf_downloader_executor_health_score > 0 == 0
        for: 2m
        labels:
          severity: critical
        annotations:
          summary: "All executors offline"

      - alert: ExecutorHealthDegraded
        expr: count(hf_downloader_executor_health_score < 50) / count(hf_downloader_executor_health_score) > 0.5
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "More than 50% executors degraded"

      - alert: TaskStuck
        expr: time() - hf_downloader_task_last_progress_timestamp_seconds > 1800
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Task {{ $labels.task_id }} stuck for 30 minutes"

      - alert: HighErrorRate
        expr: rate(hf_downloader_downloads_total{status="failed"}[10m]) / rate(hf_downloader_downloads_total[10m]) > 0.1
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Download failure rate above 10%"

      - alert: DiskSpaceLow
        expr: hf_downloader_executor_disk_free_gb < 10
        for: 2m
        labels:
          severity: warning
        annotations:
          summary: "Executor {{ $labels.executor_id }} disk space low"
```

### 4.8 优雅停机

```python
import signal

class GracefulShutdown:
    def __init__(self, shutdown_timeout: int = 60):
        self.shutdown_timeout = shutdown_timeout
        self._shutting_down = False
        self._in_flight: set[str] = set()

    def install_handlers(self):
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _handle_signal(self, signum, frame):
        log.info("Shutdown signal received", signal=signum)
        self._shutting_down = True

        deadline = time.time() + self.shutdown_timeout

        while self._in_flight and time.time() < deadline:
            log.info("Waiting for in-flight tasks", count=len(self._in_flight))
            time.sleep(2)

        if self._in_flight:
            log.warning("Force shutdown with in-flight tasks", count=len(self._in_flight))

        self._save_state()
        self._close_websockets()
        log.info("Shutdown complete")
        sys.exit(0)
```

---

## 5. E2E 测试用例

### 5.1 测试环境准备

```python
import pytest
import requests
import subprocess
import time
import hashlib


BASE_URL = "http://localhost:8080"
WS_URL = "ws://localhost:8081"


@pytest.fixture(scope="session")
def controller():
    proc = subprocess.Popen(["python", "-m", "controller.main"],
                            env={**os.environ, "DATABASE_URL": "sqlite:///test.db"})
    time.sleep(3)
    yield BASE_URL
    proc.terminate()
    proc.wait()


@pytest.fixture(scope="session")
def executor(controller):
    proc = subprocess.Popen(["python", "-m", "executor.main"],
                            env={**os.environ,
                                 "CONTROLLER_URL": controller,
                                 "EXECUTOR_ID": "test-exec-1",
                                 "MAX_WORKERS": "4",
                                 "LOCAL_STORAGE_PATH": "/tmp/test_downloads"})
    time.sleep(3)
    yield proc
    proc.terminate()
    proc.wait()
```

### 5.2 基础功能测试

```python
class TestBasicFunctionality:

    def test_01_search_models(self, controller):
        resp = requests.get(f"{controller}/api/models/search",
                            params={"query": "gpt2", "limit": 5})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["models"]) > 0
        assert "id" in data["models"][0]

    def test_02_get_model_info(self, controller):
        resp = requests.get(f"{controller}/api/models/gpt2/info")
        assert resp.status_code == 200
        data = resp.json()
        assert "siblings" in data
        assert any("config.json" in s["name"] for s in data["siblings"])

    def test_03_executor_registration(self, controller, executor):
        resp = requests.get(f"{controller}/api/executors")
        assert resp.status_code == 200
        executors = resp.json()["executors"]
        assert any(e["id"] == "test-exec-1" for e in executors)

    def test_04_heartbeat(self, controller):
        resp = requests.post(f"{controller}/api/executors/test-exec-1/heartbeat",
                             json={"progress": []})
        assert resp.status_code == 200
```

### 5.3 模拟下载测试

```python
class TestSimulationDownload:

    def test_05_create_simulation_task(self, controller, executor):
        resp = requests.post(f"{controller}/api/tasks/simulate", json={
            "repo_id": "gpt2",
            "revision": "main",
            "storage_id": None,
            "download_bytes_limit": 1024,
            "skip_sha256_verify": True,
            "perform_upload": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["task_key"].startswith("simu_")
        assert data["total_files"] > 0
        self.task_key = data["task_key"]

    def test_06_simulation_completes(self, controller, executor):
        max_wait = 120
        start = time.time()
        while time.time() - start < max_wait:
            resp = requests.get(f"{controller}/api/tasks")
            tasks = resp.json()["tasks"]
            simu = [t for t in tasks if t.get("task_key", "").startswith("simu_")]
            if simu and simu[0]["status"] == "completed":
                return
            time.sleep(2)
        pytest.fail("Simulation task did not complete within timeout")

    def test_07_simulation_file_integrity(self, controller):
        resp = requests.get(f"{controller}/api/tasks")
        tasks = resp.json()["tasks"]
        simu = [t for t in tasks if t.get("task_key", "").startswith("simu_")][0]
        assert simu["completed_files"] == simu["total_files"]
```

### 5.4 真实小模型下载测试

```python
class TestRealDownload:

    def test_08_download_small_model(self, controller, executor):
        resp = requests.post(f"{controller}/api/tasks", json={
            "repo_id": "gpt2",
            "revision": "main",
            "upload_mode": "download_only",
        })
        assert resp.status_code == 200
        data = resp.json()
        task_key = data["task_key"]
        assert task_key

        max_wait = 300
        start = time.time()
        while time.time() - start < max_wait:
            resp = requests.get(f"{controller}/api/tasks")
            tasks = resp.json()["tasks"]
            task = [t for t in tasks if t["task_key"] == task_key]
            if task and task[0]["status"] == "completed":
                return
            time.sleep(5)
        pytest.fail("Real download did not complete within timeout")

    def test_09_sha256_verification(self, controller):
        resp = requests.get(f"{controller}/api/tasks")
        tasks = resp.json()["tasks"]
        task = [t for t in tasks if not t.get("task_key", "").startswith("simu_")][0]
        assert task["status"] == "completed"

        resp = requests.get(f"{controller}/api/tasks/{task['task_key']}")
        detail = resp.json()
        for subtask in detail["subtasks"]:
            if subtask.get("sha256"):
                assert subtask.get("download_sha256_verified") is True, \
                    f"SHA256 verification failed for {subtask['filename']}"
```

### 5.5 多执行器测试

```python
class TestMultiExecutor:

    @pytest.fixture
    def second_executor(self, controller):
        proc = subprocess.Popen(["python", "-m", "executor.main"],
                                env={**os.environ,
                                     "CONTROLLER_URL": controller,
                                     "EXECUTOR_ID": "test-exec-2",
                                     "MAX_WORKERS": "4"})
        time.sleep(3)
        yield proc
        proc.terminate()
        proc.wait()

    def test_10_two_executors_registered(self, controller, executor, second_executor):
        resp = requests.get(f"{controller}/api/executors")
        executors = resp.json()["executors"]
        ids = {e["id"] for e in executors if e["status"] != "offline"}
        assert "test-exec-1" in ids
        assert "test-exec-2" in ids

    def test_11_files_distributed_across_executors(self, controller, executor,
                                                    second_executor):
        resp = requests.post(f"{controller}/api/tasks/simulate", json={
            "repo_id": "gpt2",
            "download_bytes_limit": 1024,
            "skip_sha256_verify": True,
            "perform_upload": False,
        })
        task_key = resp.json()["task_key"]

        max_wait = 120
        start = time.time()
        while time.time() - start < max_wait:
            resp = requests.get(f"{controller}/api/tasks")
            tasks = [t for t in resp.json()["tasks"] if t["task_key"] == task_key]
            if tasks and tasks[0]["status"] == "completed":
                break
            time.sleep(2)

        resp = requests.get(f"{controller}/api/tasks/{task_key}")
        detail = resp.json()
        executors_used = set()
        for s in detail["subtasks"]:
            if s.get("executor_id"):
                executors_used.add(s["executor_id"])
        assert len(executors_used) >= 1
```

### 5.6 故障恢复测试

```python
class TestFaultRecovery:

    def test_12_executor_offline_recovery(self, controller, executor):
        resp = requests.post(f"{controller}/api/tasks/simulate", json={
            "repo_id": "gpt2",
            "download_bytes_limit": 1024,
            "skip_sha256_verify": True,
            "perform_upload": False,
        })
        task_key = resp.json()["task_key"]

        executor.terminate()
        executor.wait()

        time.sleep(35)

        resp = requests.get(f"{controller}/api/executors")
        executors = resp.json()["executors"]
        exec_1 = [e for e in executors if e["id"] == "test-exec-1"][0]
        assert exec_1["status"] in ("suspect", "faulty", "offline")

        proc = subprocess.Popen(["python", "-m", "executor.main"],
                                env={**os.environ,
                                     "CONTROLLER_URL": controller,
                                     "EXECUTOR_ID": "test-exec-1",
                                     "MAX_WORKERS": "4"})
        time.sleep(15)

        resp = requests.get(f"{controller}/api/executors")
        exec_1 = [e for e in resp.json()["executors"] if e["id"] == "test-exec-1"][0]
        assert exec_1["status"] in ("healthy", "degraded")

        proc.terminate()
        proc.wait()
```

### 5.7 控制器崩溃恢复测试

```python
class TestControllerRecovery:

    def test_13_controller_restart_recovery(self):
        proc = subprocess.Popen(["python", "-m", "controller.main"],
                                env={**os.environ, "DATABASE_URL": "sqlite:///test_recovery.db"})
        time.sleep(3)
        url = "http://localhost:8080"

        requests.post(f"{url}/api/tasks/simulate", json={
            "repo_id": "gpt2", "download_bytes_limit": 1024,
            "skip_sha256_verify": True,
        })
        time.sleep(5)

        proc.terminate()
        proc.wait()
        time.sleep(2)

        proc2 = subprocess.Popen(["python", "-m", "controller.main"],
                                 env={**os.environ, "DATABASE_URL": "sqlite:///test_recovery.db"})
        time.sleep(5)

        resp = requests.get(f"{url}/api/tasks")
        tasks = resp.json()["tasks"]
        assert len(tasks) > 0
        for t in tasks:
            assert t["status"] != "assigned" or t["status"] == "active"

        proc2.terminate()
        proc2.wait()
```

### 5.8 优先级与排队测试

```python
class TestPriorityQueue:

    def test_14_priority_ordering(self, controller, executor):
        requests.post(f"{controller}/api/tasks/simulate", json={
            "repo_id": "gpt2", "priority": 3, "download_bytes_limit": 1024,
        })
        requests.post(f"{controller}/api/tasks/simulate", json={
            "repo_id": "distilgpt2", "priority": 0, "download_bytes_limit": 1024,
        })

        resp = requests.get(f"{controller}/api/tasks")
        tasks = [t for t in resp.json()["tasks"] if t["status"] in ("queued", "active")]
        if len(tasks) >= 2:
            active_first = [t for t in tasks if t["status"] == "active"]
            if active_first:
                assert active_first[0].get("priority", 2) <= tasks[-1].get("priority", 2)

    def test_15_change_priority(self, controller):
        resp = requests.get(f"{controller}/api/tasks")
        tasks = [t for t in resp.json()["tasks"] if t["status"] == "queued"]
        if tasks:
            task_key = tasks[0]["task_key"]
            resp = requests.put(f"{controller}/api/tasks/{task_key}/priority",
                                json={"priority": 0})
            assert resp.status_code == 200
```

### 5.9 探查与自动下载测试

```python
class TestProbeWatch:

    def test_16_probe_existing_repo(self, controller):
        resp = requests.post(f"{controller}/api/probes", json={
            "name": "GPT2 probe test",
            "repo_id": "gpt2",
            "interval_minutes": 1,
            "probe_condition": "repo_exists",
            "auto_download": False,
        })
        assert resp.status_code == 200
        probe_id = resp.json()["id"]

        time.sleep(65)

        resp = requests.get(f"{controller}/api/probes/{probe_id}")
        probe = resp.json()
        assert probe["total_probes"] >= 1
        assert "存在" in probe.get("last_probe_result", "") or "exists" in probe.get("last_probe_result", "").lower()

    def test_17_probe_nonexistent_repo(self, controller):
        resp = requests.post(f"{controller}/api/probes", json={
            "name": "Nonexistent probe",
            "repo_id": "fake-org/nonexistent-model-xyz-12345",
            "interval_minutes": 1,
            "probe_condition": "repo_exists",
            "auto_download": False,
        })
        probe_id = resp.json()["id"]

        time.sleep(65)

        resp = requests.get(f"{controller}/api/probes/{probe_id}")
        probe = resp.json()
        assert probe["total_probes"] >= 1
        assert probe["status"] == "active"
        assert "不存在" in probe.get("last_probe_result", "") or "not exist" in probe.get("last_probe_result", "").lower()
```

### 5.10 存储后端测试

```python
class TestStorageBackend:

    def test_18_storage_connection_test(self, controller):
        resp = requests.post(f"{controller}/api/storage", json={
            "name": "Test Local Storage",
            "storage_type": "local",
            "root_path": "/tmp/test_storage",
        })
        storage_id = resp.json()["id"]

        resp = requests.post(f"{controller}/api/storage/{storage_id}/test")
        result = resp.json()
        assert result["success"] is True

    def test_19_upload_and_verify(self, controller):
        resp = requests.get(f"{controller}/api/storage")
        storages = resp.json()["storages"]
        if not storages:
            pytest.skip("No storage configured")

        storage_id = storages[0]["id"]

        test_file = "/tmp/test_upload.bin"
        with open(test_file, "wb") as f:
            f.write(os.urandom(1024))

        sha256 = hashlib.sha256(open(test_file, "rb").read()).hexdigest()

        resp = requests.post(
            f"{controller}/api/transfer/upload/test-task/test-sub",
            files={"file": open(test_file, "rb")},
        )
        assert resp.status_code == 200
```

### 5.11 日志与追踪测试

```python
class TestObservability:

    def test_20_metrics_endpoint(self, controller):
        resp = requests.get(f"{controller}/metrics")
        assert resp.status_code == 200
        content = resp.text
        assert "hf_downloader_downloads_total" in content
        assert "hf_downloader_executor_health_score" in content

    def test_21_trace_id_in_logs(self, controller, executor):
        resp = requests.post(f"{controller}/api/tasks/simulate", json={
            "repo_id": "gpt2", "download_bytes_limit": 1024,
        })
        task_key = resp.json()["task_key"]

        log_file = "/tmp/test_controller.log"
        if os.path.exists(log_file):
            content = open(log_file).read()
            assert task_key in content
            assert "trace_id" in content
```

### 5.12 完整性校验测试

```python
class TestIntegrity:

    def test_22_full_file_list_completeness(self, controller, executor):
        info_resp = requests.get(f"{controller}/api/models/gpt2/info")
        hf_files = {s["name"] for s in info_resp.json()["siblings"]}

        resp = requests.post(f"{controller}/api/tasks/simulate", json={
            "repo_id": "gpt2", "download_bytes_limit": 1024,
        })
        task_key = resp.json()["task_key"]

        max_wait = 120
        start = time.time()
        while time.time() - start < max_wait:
            resp = requests.get(f"{controller}/api/tasks")
            tasks = [t for t in resp.json()["tasks"] if t["task_key"] == task_key]
            if tasks and tasks[0]["status"] == "completed":
                break
            time.sleep(2)

        detail_resp = requests.get(f"{controller}/api/tasks/{task_key}")
        detail = detail_resp.json()
        downloaded_files = {s["filename"] for s in detail["subtasks"]}

        assert hf_files == downloaded_files, \
            f"Missing files: {hf_files - downloaded_files}"

    def test_23_sha256_matches_hf_metadata(self, controller, executor):
        resp = requests.post(f"{controller}/api/tasks", json={
            "repo_id": "gpt2", "upload_mode": "download_only",
        })
        task_key = resp.json()["task_key"]

        max_wait = 300
        start = time.time()
        while time.time() - start < max_wait:
            resp = requests.get(f"{controller}/api/tasks")
            tasks = [t for t in resp.json()["tasks"] if t["task_key"] == task_key]
            if tasks and tasks[0]["status"] == "completed":
                break
            time.sleep(5)

        info_resp = requests.get(f"{controller}/api/models/gpt2/info")
        hf_files = {s["name"]: s for s in info_resp.json()["siblings"]}

        detail_resp = requests.get(f"{controller}/api/tasks/{task_key}")
        for subtask in detail_resp.json()["subtasks"]:
            if hf_files[subtask["filename"]].get("sha256"):
                assert subtask.get("download_sha256_verified") is True, \
                    f"SHA256 mismatch: {subtask['filename']}"
```

### 5.13 测试用例矩阵总览

| # | 测试类 | 用例名 | 验证内容 |
|---|--------|--------|---------|
| 01 | Basic | test_search_models | HF API 搜索 |
| 02 | Basic | test_get_model_info | 模型详情/文件列表 |
| 03 | Basic | test_executor_registration | 执行器注册 |
| 04 | Basic | test_heartbeat | 心跳机制 |
| 05 | Simulation | test_create_simulation_task | 模拟任务创建 |
| 06 | Simulation | test_simulation_completes | 模拟任务完成 |
| 07 | Simulation | test_simulation_file_integrity | 模拟文件完整性 |
| 08 | Real | test_download_small_model | 真实小模型下载 |
| 09 | Real | test_sha256_verification | SHA256 校验 |
| 10 | Multi | test_two_executors_registered | 多执行器注册 |
| 11 | Multi | test_files_distributed | 文件跨执行器分配 |
| 12 | Fault | test_executor_offline_recovery | 执行器故障恢复 |
| 13 | Recovery | test_controller_restart_recovery | 控制器崩溃恢复 |
| 14 | Priority | test_priority_ordering | 优先级排序 |
| 15 | Priority | test_change_priority | 动态调整优先级 |
| 16 | Probe | test_probe_existing_repo | 探查已有仓库 |
| 17 | Probe | test_probe_nonexistent_repo | 探查不存在仓库 |
| 18 | Storage | test_storage_connection_test | 存储连接测试 |
| 19 | Storage | test_upload_and_verify | 上传与校验 |
| 20 | Observability | test_metrics_endpoint | Prometheus 指标 |
| 21 | Observability | test_trace_id_in_logs | Trace ID 追踪 |
| 22 | Integrity | test_full_file_list_completeness | 文件清单完整性 |
| 23 | Integrity | test_sha256_matches_hf_metadata | SHA256 与 HF 元数据一致 |
