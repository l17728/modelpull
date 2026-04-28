# [SUPERSEDED] 故障治愈 · 动态负载均衡 · 可视化 — 补充设计文档 v1.4

> ⚠️ **此文档已被 v2.0 取代，仅作历史追溯**
>
> 当前权威文档：**[../v2.0/00-INDEX.md](../v2.0/00-INDEX.md)**
>
> 本文档内容已分别合并到：
> - 节点状态机 / 故障治愈 → `../v2.0/01-architecture.md` §3.3 + `../v2.0/03-distributed-correctness.md` §5
> - 多级重试 → `../v2.0/03-distributed-correctness.md` §8
> - UI 详细设计（含 §6.4 / §6.8 / §12.5 重复矩阵已合并） → `../v2.0/06-platform-and-ecosystem.md` §7
> - 状态机（§4.1 / §8.6 自相矛盾的两版图） → 已统一为 `../v2.0/01-architecture.md` §3
>
> **请勿基于本文档实施。** 实施时以 v2.0 为准。

> 版本: v1.4（已废弃）| 原日期: 2026-04-28

---

## 目录

1. [故障治愈与节点健康管理系统](#1-故障治愈与节点健康管理系统)
2. [多级重试策略](#2-多级重试策略)
3. [动态负载均衡引擎](#3-动态负载均衡引擎)
4. [任务状态机与生命周期](#4-任务状态机与生命周期)
5. [实时进度汇报协议](#5-实时进度汇报协议)
6. [可视化UI详细设计](#6-可视化ui详细设计)
7. [全速下载优化策略](#7-全速下载优化策略)
8. [存储后端配置与自动装配](#8-存储后端配置与自动装配)
9. [任务优先级与排队调度](#9-任务优先级与排队调度)
10. [定时探查与自动下载](#10-定时探查与自动下载)
11. [模型完整文件清单策略](#11-模型完整文件清单策略)
12. [模拟测试模式](#12-模拟测试模式)

---

## 1. 故障治愈与节点健康管理系统

### 1.1 节点健康状态机

```
                    ┌──────────┐
          注册成功  │          │  首次心跳
         ────────> │ joining  │ ──────────>
                    │          │
                    └──────────┘
                                    │
                                    ▼
                    ┌──────────────────────────────────┐
              ┌───> │            healthy               │ <───┐
              │     │  (正常接收任务,正常下载)            │     │
              │     └──────┬──────────────┬────────────┘     │
              │            │              │                   │
              │     单次任务失败    连续心跳超时           探测恢复
              │            │              │                   │
              │            ▼              ▼                   │
              │     ┌──────────┐   ┌──────────┐              │
              │     │ degraded │   │ suspect  │──────────────┘
              │     │(降级运行) │   │(疑似故障) │
              │     └────┬─────┘   └────┬──────┘
              │          │              │ 确认故障
              │    连续N次任务成功      │
              │          │              ▼
              │          │        ┌──────────┐
              │          │        │  faulty  │
              │          └────────│ (故障隔离)│
              │                   └────┬─────┘
              │                        │
              │           间断性探测恢复 │
              │                        ▼
              │                  ┌───────────┐
              │                  │ probing   │
              │                  │ (探测恢复) │
              │                  └────┬──────┘
              │                       │ 探测成功
              └───────────────────────┘
```

### 1.2 节点健康档案

```python
@dataclass
class NodeHealthProfile:
    executor_id: str
    status: str                          # healthy / degraded / suspect / faulty / probing
    last_heartbeat: datetime

    # 统计指标
    total_tasks_assigned: int
    total_tasks_completed: int
    total_tasks_failed: int
    consecutive_successes: int = 0
    consecutive_failures: int = 0

    # 速率追踪
    speed_samples: deque[SpeedSample]            # 最近 60 个速度采样
    avg_speed_bps: float = 0.0
    peak_speed_bps: float = 0.0
    effective_bandwidth: float = 0.0             # 加权有效带宽(考虑故障率)

    # 故障追踪
    failure_history: list[FailureRecord]          # 最近 50 条故障记录
    last_failure_at: datetime | None = None
    last_failure_reason: str | None = None

    # 探测
    probe_interval: int = 60                      # 探测间隔(秒), 逐步递增
    next_probe_at: datetime | None = None
    probe_failures: int = 0
    probe_successes: int = 0

    # 健康评分 (0-100)
    health_score: float = 100.0

    # 动态参数(控制器下发)
    max_concurrent_files: int = 3
    chunk_threads_per_file: int = 8
    chunk_size: int = 256 * 1024 * 1024           # 256MB

    @property
    def success_rate(self) -> float:
        if self.total_tasks_assigned == 0:
            return 1.0
        return self.total_tasks_completed / self.total_tasks_assigned

    @property
    def can_accept_tasks(self) -> bool:
        return self.status in ("healthy", "degraded")

    @property
    def task_capacity_weight(self) -> float:
        """用于负载均衡的权重, 考虑健康度和速率"""
        base = self.health_score / 100.0
        if self.status == "degraded":
            base *= 0.5
        return base * self.effective_bandwidth


@dataclass
class FailureRecord:
    timestamp: datetime
    subtask_id: str
    filename: str
    error_type: str              # network_error / timeout / http_429 / sha256_mismatch / unknown
    error_message: str
    bytes_downloaded: int
    bytes_total: int
    retry_attempt: int


@dataclass
class SpeedSample:
    timestamp: datetime
    speed_bps: float
    active_files: int
    active_threads: int
```

### 1.3 健康评分算法

```python
class HealthScorer:
    WEIGHTS = {
        "success_rate": 0.30,
        "heartbeat_stability": 0.20,
        "speed_consistency": 0.25,
        "recent_failures": 0.25,
    }

    def compute_score(self, profile: NodeHealthProfile) -> float:
        score = 100.0

        # 因子1: 任务成功率 (权重 30%)
        success_factor = profile.success_rate * 100

        # 因子2: 心跳稳定性 (权重 20%)
        heartbeat_factor = 100.0
        if profile.last_heartbeat:
            gap = (datetime.utcnow() - profile.last_heartbeat).total_seconds()
            if gap > 30:
                heartbeat_factor = max(0, 100 - (gap - 30) * 2)

        # 因子3: 速度一致性 (权重 25%) — 速度波动越小越好
        speed_factor = 100.0
        if len(profile.speed_samples) >= 5:
            speeds = [s.speed_bps for s in profile.speed_samples]
            mean_speed = statistics.mean(speeds)
            if mean_speed > 0:
                cv = statistics.stdev(speeds) / mean_speed   # 变异系数
                speed_factor = max(0, 100 - cv * 100)

        # 因子4: 近期故障惩罚 (权重 25%)
        failure_factor = 100.0
        recent_failures = [
            f for f in profile.failure_history
            if (datetime.utcnow() - f.timestamp).total_seconds() < 600  # 最近10分钟
        ]
        failure_factor = max(0, 100 - len(recent_failures) * 25)
        if profile.consecutive_failures > 0:
            failure_factor *= (0.5 ** profile.consecutive_failures)    # 指数衰减

        score = (
            success_factor * self.WEIGHTS["success_rate"]
            + heartbeat_factor * self.WEIGHTS["heartbeat_stability"]
            + speed_factor * self.WEIGHTS["speed_consistency"]
            + failure_factor * self.WEIGHTS["recent_failures"]
        )

        return max(0.0, min(100.0, score))
```

### 1.4 节点状态转换引擎

```python
class NodeStateManager:
    def __init__(self, config: SystemConfig):
        self.profiles: dict[str, NodeHealthProfile] = {}
        self.config = config
        self._probe_scheduler = ProbeScheduler()

    def on_task_completed(self, executor_id: str, subtask_id: str, speed_bps: float):
        p = self._get_profile(executor_id)
        p.total_tasks_completed += 1
        p.consecutive_successes += 1
        p.consecutive_failures = 0
        p.speed_samples.append(SpeedSample(
            timestamp=datetime.utcnow(), speed_bps=speed_bps,
            active_files=p.max_concurrent_files, active_threads=p.chunk_threads_per_file,
        ))
        p.avg_speed_bps = statistics.mean(s.speed_bps for s in p.speed_samples)
        p.peak_speed_bps = max(p.peak_speed_bps, speed_bps)
        p.effective_bandwidth = p.avg_speed_bps
        self._update_health_score(p)

        # degraded → healthy: 连续3次成功恢复
        if p.status == "degraded" and p.consecutive_successes >= 3:
            self._transition(p, "healthy")
            log.info(f"Executor {executor_id} recovered to healthy")

    def on_task_failed(self, executor_id: str, subtask_id: str,
                       error_type: str, error_message: str,
                       retry_attempt: int):
        p = self._get_profile(executor_id)
        p.total_tasks_failed += 1
        p.consecutive_failures += 1
        p.consecutive_successes = 0
        p.last_failure_at = datetime.utcnow()
        p.last_failure_reason = error_message
        p.failure_history.append(FailureRecord(
            timestamp=datetime.utcnow(), subtask_id=subtask_id,
            filename=subtask_id, error_type=error_type,
            error_message=error_message, bytes_downloaded=0,
            bytes_total=0, retry_attempt=retry_attempt,
        ))
        if len(p.failure_history) > 50:
            p.failure_history = p.failure_history[-50:]
        self._update_health_score(p)

        # 状态转换逻辑
        if p.status == "healthy":
            if p.consecutive_failures >= 1:
                self._transition(p, "degraded")
        elif p.status == "degraded":
            if p.consecutive_failures >= 3:
                self._transition(p, "suspect")
        elif p.status == "suspect":
            if p.consecutive_failures >= 2:
                self._transition(p, "faulty")
                self._isolate_node(p)

    def on_heartbeat_timeout(self, executor_id: str):
        p = self._get_profile(executor_id)
        if p.status in ("healthy", "degraded"):
            self._transition(p, "suspect")
        elif p.status == "suspect":
            self._transition(p, "faulty")
            self._isolate_node(p)

    def on_heartbeat_received(self, executor_id: str):
        p = self._get_profile(executor_id)
        p.last_heartbeat = datetime.utcnow()
        if p.status == "suspect":
            self._transition(p, "degraded")

    def _isolate_node(self, profile: NodeHealthProfile):
        """故障隔离: 回收所有未完成任务, 加入探测队列"""
        profile.can_accept_tasks = False
        self._reclaim_tasks(profile.executor_id)
        self._probe_scheduler.schedule_probe(profile, initial_interval=60)
        log.warning(f"Executor {profile.executor_id} isolated, tasks reclaimed")

    def _transition(self, profile: NodeHealthProfile, new_status: str):
        old = profile.status
        profile.status = new_status
        log.info(f"Executor {profile.executor_id}: {old} → {new_status} "
                 f"(score={profile.health_score:.1f})")
        self._emit_node_status_event(profile, old, new_status)

    def _update_health_score(self, profile: NodeHealthProfile):
        profile.health_score = HealthScorer().compute_score(profile)
        self._adjust_executor_params(profile)

    def _adjust_executor_params(self, profile: NodeHealthProfile):
        """根据健康度动态调整执行器参数"""
        score = profile.health_score
        if score >= 80:
            profile.max_concurrent_files = self.config.default_max_files
            profile.chunk_threads_per_file = self.config.default_chunk_threads
        elif score >= 50:
            profile.max_concurrent_files = max(1, self.config.default_max_files - 1)
            profile.chunk_threads_per_file = max(2, self.config.default_chunk_threads // 2)
        else:
            profile.max_concurrent_files = 1
            profile.chunk_threads_per_file = 2
```

### 1.5 探测恢复调度器

```python
class ProbeScheduler:
    """
    间断性探测恢复: 对故障节点逐步增加探测间隔
    探测成功3次 → 恢复为 healthy
    探测失败   → 加倍探测间隔, 最长 30 分钟
    """
    MAX_PROBE_INTERVAL = 1800     # 30 分钟
    PROBE_SUCCESS_THRESHOLD = 3   # 连续成功3次恢复

    def __init__(self):
        self._probes: dict[str, ProbeState] = {}
        self._running = False

    def schedule_probe(self, profile: NodeHealthProfile, initial_interval: int = 60):
        self._probes[profile.executor_id] = ProbeState(
            executor_id=profile.executor_id,
            interval=initial_interval,
            next_probe_at=datetime.utcnow() + timedelta(seconds=initial_interval),
            consecutive_successes=0,
            consecutive_failures=0,
        )
        if not self._running:
            self._running = True
            threading.Thread(target=self._probe_loop, daemon=True).start()

    def _probe_loop(self):
        while self._running:
            now = datetime.utcnow()
            for eid, probe in list(self._probes.items()):
                if now >= probe.next_probe_at:
                    self._execute_probe(eid, probe)
            time.sleep(5)

    def _execute_probe(self, executor_id: str, probe: ProbeState):
        try:
            resp = requests.get(
                f"{self._get_executor_url(executor_id)}/health",
                timeout=10
            )
            if resp.status_code == 200:
                probe.consecutive_successes += 1
                probe.consecutive_failures = 0

                if probe.consecutive_successes >= self.PROBE_SUCCESS_THRESHOLD:
                    self._on_probe_recovered(executor_id, probe)
                    return
            else:
                raise Exception(f"HTTP {resp.status_code}")
        except Exception as e:
            probe.consecutive_successes = 0
            probe.consecutive_failures += 1
            # 指数退避探测间隔
            probe.interval = min(probe.interval * 2, self.MAX_PROBE_INTERVAL)
            log.info(f"Probe failed for {executor_id}: {e}, "
                     f"next probe in {probe.interval}s")

        probe.next_probe_at = datetime.utcnow() + timedelta(seconds=probe.interval)

    def _on_probe_recovered(self, executor_id: str, probe: ProbeState):
        """节点恢复: 重新加入下载序列"""
        del self._probes[executor_id]
        profile = self.node_manager.get_profile(executor_id)
        profile.status = "healthy"
        profile.health_score = 70.0     # 初始恢复给 70 分
        profile.consecutive_failures = 0
        profile.consecutive_successes = 0
        log.info(f"Executor {executor_id} PROBE RECOVERED, rejoining cluster")
        self.load_balancer.trigger_rebalance(reason=f"node_recovered:{executor_id}")


@dataclass
class ProbeState:
    executor_id: str
    interval: int
    next_probe_at: datetime
    consecutive_successes: int = 0
    consecutive_failures: int = 0
```

---

## 2. 多级重试策略

### 2.1 重试层级

```
Level 0: Chunk 级重试 (Executor 内部, 单个分块下载失败)
  ├─ 网络超时 / 连接断开 → 重试该 chunk, 最多 5 次, 指数退避
  ├─ HTTP 429 (限流)    → 指数退避, 1s → 2s → 4s → 8s → 16s
  └─ HTTP 5xx           → 重试 3 次

Level 1: File 级重试 (Executor 内部, 整个文件失败)
  ├─ SHA256 校验失败     → 删除临时文件, 从头重新下载, 最多 3 次
  ├─ 多 chunk 反复失败   → 降低线程数重试
  └─ 文件下载超时        → 增加超时时间重试

Level 2: Executor 级重试 (Controller 管理, 执行器节点问题)
  ├─ 执行器报告任务失败  → 重新分配给同一执行器, 最多 2 次
  └─ 2 次失败后         → 分配给其他执行器

Level 3: Global 级重试 (Controller 管理, 终极保障)
  ├─ 所有执行器都失败   → 标记为 problem_file, 降低优先级延后重试
  └─ problem_file 重试  → 单线程慢速重试, 避免触发限流
```

### 2.2 重试策略实现

```python
class RetryPolicy:
    def __init__(self):
        self.chunk_retries = 5
        self.file_retries = 3
        self.executor_retries = 2
        self.global_retries = 5

    def get_backoff_delay(self, level: str, attempt: int, error_type: str) -> float:
        if error_type == "http_429":
            return min(2.0 ** attempt, 60.0)

        base_delays = {
            "chunk": 1.0,
            "file": 5.0,
            "executor": 10.0,
            "global": 30.0,
        }
        base = base_delays.get(level, 5.0)
        delay = base * (2.0 ** min(attempt, 5))
        jitter = random.uniform(0.8, 1.2)
        return delay * jitter


class RetryExecutor:
    def __init__(self, node_manager: NodeStateManager, policy: RetryPolicy):
        self.node_manager = node_manager
        self.policy = policy
        self.file_retry_count: dict[str, int] = {}           # subtask_id → count
        self.executor_retry_count: dict[tuple[str, str], int] = {}  # (subtask_id, executor_id) → count
        self.global_retry_count: dict[str, int] = {}          # subtask_id → count
        self.problem_files: dict[str, ProblemFileRecord] = {}

    def on_task_failed(self, subtask: FileSubTask, executor_id: str,
                       error_type: str, error_message: str):
        subtask_id = subtask.id

        # Level 2: Executor 级 — 换执行器重试
        executor_key = (subtask_id, executor_id)
        self.executor_retry_count[executor_key] = \
            self.executor_retry_count.get(executor_key, 0) + 1

        if self.executor_retry_count[executor_key] <= self.policy.executor_retries:
            self.node_manager.on_task_failed(executor_id, subtask_id, error_type, error_message, 0)
            self._reassign_to_different_executor(subtask, exclude={executor_id})
            return

        # Level 3: Global 级 — 终极重试
        self.global_retry_count[subtask_id] = \
            self.global_retry_count.get(subtask_id, 0) + 1

        if self.global_retry_count[subtask_id] <= self.policy.global_retries:
            if self.global_retry_count[subtask_id] >= 3:
                self.problem_files[subtask_id] = ProblemFileRecord(
                    subtask_id=subtask_id,
                    filename=subtask.filename,
                    fail_count=self.global_retry_count[subtask_id],
                    last_error=error_message,
                    status="slow_retry",
                )
                self._slow_retry(subtask)
            else:
                self._reassign_to_different_executor(subtask)
            return

        # 彻底失败
        subtask.status = "failed_permanent"
        log.error(f"PERMANENT FAILURE: {subtask.filename} after "
                  f"{self.global_retry_count[subtask_id]} global retries")

    def _reassign_to_different_executor(self, subtask: FileSubTask,
                                         exclude: set[str] = None):
        subtask.status = "pending"
        subtask.executor_id = None
        subtask.downloaded_bytes = 0
        candidates = [
            e for e in self.node_manager.get_available_executors()
            if e.id not in (exclude or set())
        ]
        if candidates:
            best = max(candidates, key=lambda e: e.task_capacity_weight)
            self._assign(subtask, best)
        else:
            log.warning(f"No available executor for subtask {subtask.id}, queued")

    def _slow_retry(self, subtask: FileSubTask):
        """单线程慢速重试 problem file"""
        subtask.status = "slow_retry"
        subtask.slow_retry = True
        self._reassign_to_different_executor(subtask)


@dataclass
class ProblemFileRecord:
    subtask_id: str
    filename: str
    fail_count: int
    last_error: str
    status: str              # slow_retry / abandoned
```

---

## 3. 动态负载均衡引擎

### 3.1 触发条件

```python
class LoadBalanceEngine:
    """
    动态负载均衡触发条件:
    1. 新执行器注册           → rebalance
    2. 执行器故障隔离         → rebalance (回收任务重新分配)
    3. 执行器恢复             → rebalance
    4. 执行器速度显著变化     → rebalance (>30% 偏离均值)
    5. 任务完成(文件级)       → micro-rebalance (给空闲执行器补充任务)
    6. 定时周期性检查         → rebalance-check (每 60 秒)
    """

    SPEED_DEVIATION_THRESHOLD = 0.30     # 30% 偏离触发
    REBALANCE_CHECK_INTERVAL = 60        # 秒

    def __init__(self, node_manager: NodeStateManager):
        self.node_manager = node_manager
        self._rebalance_lock = threading.Lock()
        self._last_rebalance = datetime.min
        self._min_rebalance_gap = 10       # 最短 10 秒间隔防止抖动

    def trigger_rebalance(self, reason: str):
        now = datetime.utcnow()
        if (now - self._last_rebalance).total_seconds() < self._min_rebalance_gap:
            return
        with self._rebalance_lock:
            self._last_rebalance = now
            self._execute_rebalance(reason)

    def _execute_rebalance(self, reason: str):
        log.info(f"Rebalance triggered: {reason}")
        task = self._get_active_task()
        if not task:
            return

        active_executors = self.node_manager.get_available_executors()
        if not active_executors:
            return

        pending = [s for s in task.subtasks if s.status == "pending"]
        assigned = [s for s in task.subtasks if s.status in ("assigned", "downloading")]
        downloading = [s for s in task.subtasks if s.status == "downloading"]

        # 计算每个执行器的当前负载(剩余字节数)
        loads: dict[str, float] = {}
        for e in active_executors:
            loads[e.id] = 0.0
        for s in assigned + downloading:
            if s.executor_id in loads:
                loads[s.executor_id] += max(0, s.file_size - s.downloaded_bytes)

        # 计算权重(基于有效带宽)
        weights = {}
        for e in active_executors:
            profile = self.node_manager.get_profile(e.id)
            weights[e.id] = profile.task_capacity_weight

        total_weight = sum(weights.values())
        if total_weight == 0:
            return

        # 理想负载分配
        total_remaining = sum(loads.values()) + sum(s.file_size for s in pending)
        ideal_loads = {eid: total_remaining * (w / total_weight) for eid, w in weights.items()}

        # 分配 pending 任务
        sorted_pending = sorted(pending, key=lambda s: s.file_size, reverse=True)
        for subtask in sorted_pending:
            best = min(active_executors, key=lambda e: loads[e.id] / max(weights[e.id], 1))
            self._assign_subtask(subtask, best)
            loads[best.id] += subtask.file_size

        # 检查是否需要迁移: 正在下载但严重偏离理想负载的任务
        for s in assigned:
            if s.executor_id not in loads:
                continue
            profile = self.node_manager.get_profile(s.executor_id)
            if profile.status == "degraded" and s.downloaded_bytes < s.file_size * 0.1:
                # 降级节点且刚开始的任务 → 迁移给健康节点
                healthy = [e for e in active_executors
                           if self.node_manager.get_profile(e.id).status == "healthy"]
                if healthy:
                    target = min(healthy, key=lambda e: loads[e.id] / max(weights[e.id], 1))
                    self._migrate_subtask(s, target)
                    loads[s.executor_id] -= max(0, s.file_size - s.downloaded_bytes)
                    loads[target.id] += s.file_size

        # 速度偏离检测
        self._check_speed_deviation(active_executors)

    def _check_speed_deviation(self, executors: list):
        speeds = []
        for e in executors:
            profile = self.node_manager.get_profile(e.id)
            if profile.avg_speed_bps > 0:
                speeds.append(profile.avg_speed_bps)
        if len(speeds) < 2:
            return
        mean_speed = statistics.mean(speeds)
        for e in executors:
            profile = self.node_manager.get_profile(e.id)
            if profile.avg_speed_bps > 0 and mean_speed > 0:
                deviation = abs(profile.avg_speed_bps - mean_speed) / mean_speed
                if deviation > self.SPEED_DEVIATION_THRESHOLD:
                    log.info(f"Speed deviation detected for {e.id}: "
                             f"{profile.avg_speed_bps/1e6:.1f} MB/s vs mean {mean_speed/1e6:.1f} MB/s")
                    self._update_executor_params(profile)

    def _update_executor_params(self, profile: NodeHealthProfile):
        """根据实时速度动态调整执行器并发参数"""
        current_speed_mbps = profile.avg_speed_bps / 1e6

        if current_speed_mbps > 100:        # >100 MB/s
            profile.max_concurrent_files = 5
            profile.chunk_threads_per_file = 12
            profile.chunk_size = 512 * 1024 * 1024       # 512MB
        elif current_speed_mbps > 50:       # >50 MB/s
            profile.max_concurrent_files = 4
            profile.chunk_threads_per_file = 8
            profile.chunk_size = 256 * 1024 * 1024       # 256MB
        elif current_speed_mbps > 20:       # >20 MB/s
            profile.max_concurrent_files = 3
            profile.chunk_threads_per_file = 6
            profile.chunk_size = 128 * 1024 * 1024       # 128MB
        elif current_speed_mbps > 5:        # >5 MB/s
            profile.max_concurrent_files = 2
            profile.chunk_threads_per_file = 4
            profile.chunk_size = 64 * 1024 * 1024        # 64MB
        else:                                # <5 MB/s
            profile.max_concurrent_files = 1
            profile.chunk_threads_per_file = 2
            profile.chunk_size = 32 * 1024 * 1024        # 32MB

        # 通知执行器更新参数
        self._push_param_update(profile)
```

### 3.2 负载均衡可视化决策树

```
触发事件
  │
  ├─ 新节点注册
  │    └─ pending 任务 → 按权重分配给新节点
  │
  ├─ 节点故障
  │    ├─ 该节点 assigned 尚未开始 → 立即回收, 分配给其他节点
  │    ├─ 该节点 downloading < 10% → 回收, 分配给其他节点
  │    └─ 该节点 downloading > 10% → 等待确认, 30s 后仍无心跳再回收
  │
  ├─ 节点恢复
  │    └─ 从最快完成的节点抽取 pending 任务给恢复节点
  │
  ├─ 文件完成
  │    └─ 给该执行器分配下一个 pending 文件
  │
  └─ 定时检查(60s)
       ├─ 计算各节点实际速度 vs 理想速度
       ├─ 偏离 > 30% → 调整该节点参数(线程数/并发数/chunk大小)
       └─ 某节点长期空闲 → 从最忙节点迁移任务
```

---

## 4. 任务状态机与生命周期

### 4.1 文件级子任务状态

```
                     ┌─────────┐
              创建    │         │
            ────────>│ pending │ <──────────────────┐
                     │         │                     │
                     └────┬────┘                     │
                          │ 分配给执行器               │
                          ▼                          │
                     ┌──────────┐                    │
                     │ assigned │                    │
                     │ (已分配)  │                    │ 重试(换节点)
                     └────┬─────┘                    │
                          │ 执行器开始下载              │
                          ▼                          │
                  ┌──────────────┐                   │
           ┌────>│ downloading  │───────┐           │
           │     │ (下载中)      │       │           │
           │     └──────────────┘       │           │
           │       │     │     │        │ 下载失败   │
           │       │     │     │        │           │
           │  暂停  │  失败  │ 完成     │           │
           │       │     │     │        │           │
           │       ▼     │     │        ▼           │
           │ ┌─────────┐ │     │  ┌───────────┐    │
           │ │ paused  │ │     │  │  failed    │────┘
           │ │ (暂停)   │ │     │  │ (失败)     │
           │ └────┬────┘ │     │  └─────┬─────┘
           │      │      │     │        │
           │  恢复 │      │     │  超过最大重试
           │      │      │     │        │
           │      └──────┤     │        ▼
           │             │     │  ┌──────────────────┐
           └─────────────┘     │  │ failed_permanent │
                               │  │ (永久失败)         │
                               │  └──────────────────┘
                               │
                               ▼
                        ┌───────────────┐
                        │ transferring  │
                        │ (传输到控制器)  │
                        └───────┬───────┘
                                │
                                ▼
                        ┌───────────────┐
                        │  verifying    │
                        │ (控制器校验)    │
                        └───────┬───────┘
                                │
                                ▼
                        ┌───────────────┐
                        │  completed    │
                        │ (完成)         │
                        └───────────────┘
```

### 4.2 子任务数据模型(扩展)

```python
@dataclass
class FileSubTask:
    id: str
    task_id: str
    filename: str
    file_size: int
    expected_sha256: str | None

    # 状态
    status: str = "pending"                    # 上述状态机
    executor_id: str | None = None
    downloaded_bytes: int = 0
    speed_bps: float = 0.0
    slow_retry: bool = False

    # 时间追踪
    assigned_at: datetime | None = None
    download_started_at: datetime | None = None
    download_completed_at: datetime | None = None
    transfer_completed_at: datetime | None = None
    verified_at: datetime | None = None

    # 重试
    retry_count: int = 0
    max_retries: int = 5
    last_error: str | None = None
    last_error_at: datetime | None = None
    executor_history: list[str] = field(default_factory=list)   # 曾分配过的执行器列表

    # 进度细分(执行器汇报)
    chunks_total: int = 0
    chunks_completed: int = 0
    active_threads: int = 0
    local_path: str | None = None

    # 传输
    transfer_progress_percent: float = 0.0
    transfer_speed_bps: float = 0.0

    @property
    def progress_percent(self) -> float:
        if self.file_size == 0:
            return 100.0
        return min(100.0, self.downloaded_bytes / self.file_size * 100)

    @property
    def elapsed_seconds(self) -> float:
        start = self.download_started_at or self.assigned_at
        if not start:
            return 0.0
        end = self.download_completed_at or datetime.utcnow()
        return (end - start).total_seconds()

    @property
    def status_label(self) -> str:
        labels = {
            "pending": "等待分配",
            "assigned": "已分配",
            "downloading": "下载中",
            "paused": "已暂停",
            "failed": "失败重试中",
            "failed_permanent": "永久失败",
            "transferring": "传输中",
            "verifying": "校验中",
            "completed": "已完成",
            "slow_retry": "慢速重试",
        }
        return labels.get(self.status, self.status)
```

---

## 5. 实时进度汇报协议

### 5.1 执行器 → 控制器 汇报格式

```python
@dataclass
class ProgressReport:
    executor_id: str
    timestamp: datetime
    system: SystemStatus
    tasks: list[TaskProgress]

@dataclass
class SystemStatus:
    cpu_percent: float
    memory_percent: float
    disk_free_gb: float
    network_in_bps: float                   # 实时入站速率
    network_out_bps: float
    active_downloads: int
    active_threads: int
    max_concurrent_files: int               # 当前配置参数
    chunk_threads_per_file: int
    chunk_size: int

@dataclass
class TaskProgress:
    subtask_id: str
    status: str
    downloaded_bytes: int
    file_size: int
    speed_bps: float
    chunks_total: int
    chunks_completed: int
    active_threads: int
    error_message: str | None = None
    eta_seconds: float | None = None
```

### 5.2 心跳中携带进度

```json
POST /api/executors/{executor_id}/heartbeat

{
  "executor_id": "exec-node-1",
  "timestamp": "2026-04-28T10:30:00Z",
  "system": {
    "cpu_percent": 45.2,
    "memory_percent": 62.1,
    "disk_free_gb": 850.3,
    "network_in_bps": 104857600,
    "network_out_bps": 5242880,
    "active_downloads": 3,
    "active_threads": 24,
    "max_concurrent_files": 3,
    "chunk_threads_per_file": 8,
    "chunk_size": 268435456
  },
  "tasks": [
    {
      "subtask_id": "st-001",
      "status": "downloading",
      "downloaded_bytes": 3221225472,
      "file_size": 4592318464,
      "speed_bps": 52428800,
      "chunks_total": 18,
      "chunks_completed": 12,
      "active_threads": 6,
      "eta_seconds": 26
    },
    {
      "subtask_id": "st-002",
      "status": "downloading",
      "downloaded_bytes": 1073741824,
      "file_size": 4592318464,
      "speed_bps": 31457280,
      "chunks_total": 18,
      "chunks_completed": 4,
      "active_threads": 8,
      "eta_seconds": 112
    },
    {
      "subtask_id": "st-003",
      "status": "assigned",
      "downloaded_bytes": 0,
      "file_size": 4592318464,
      "speed_bps": 0,
      "chunks_total": 0,
      "chunks_completed": 0,
      "active_threads": 0,
      "eta_seconds": null
    }
  ]
}
```

### 5.3 控制器 → 执行器 参数调整

```json
POST /api/executors/{executor_id}/config

{
  "max_concurrent_files": 4,
  "chunk_threads_per_file": 10,
  "chunk_size": 536870912,
  "reason": "speed_adaptation: detected 120MB/s bandwidth"
}
```

---

## 6. 可视化UI详细设计

### 6.1 整体架构: 多 Tab 页 + 任务队列

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  🔍 HF Distributed Downloader                                              │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌─ 顶部导航 Tab 栏 ────────────────────────────────────────────────────┐  │
│  │                                                                       │  │
│  │  [📋 任务队列(5)]  [🔍 DeepSeek-V3 ▼]  [🔍 Kimi-K2 ▼]               │  │
│  │  [🔍 GLM-4 ▼]  [🔍 DeepSeek-R1 ✅]  [+ 新建下载]                     │  │
│  │                                                                       │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                                                              │
│  ┌─ Tab 内容区 (显示当前选中Tab的详情) ──────────────────────────────────┐  │
│  │                                                                       │  │
│  │  (当前选中: DeepSeek-V3 任务详情页)                                    │  │
│  │                                                                       │  │
│  │  ...                                                                  │  │
│  │                                                                       │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

**Tab 栏规则：**
- `[📋 任务队列]` 始终固定在第一位，点击显示所有任务的概览列表
- 每个下载任务创建后自动生成一个 Tab，Tab 名为模型短名称
- Tab 上显示状态图标：`▼` 下载中 / `⏸` 暂停 / `✅` 完成 / `❌` 失败 / `🕐` 排队中
- 点击 Tab 切换到该任务的详细监控页
- Tab 可关闭（仅关闭视图，不取消任务）
- 支持同时打开多个任务 Tab，实时并行推送所有活跃任务的进度

### 6.2 Tab 0: 任务队列总览页

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  📋 任务队列                                                         [+ 新建]│
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌─ 全局状态概览 ───────────────────────────────────────────────────────┐  │
│  │                                                                       │  │
│  │  活跃任务: 3  │  排队中: 1  │  已完成: 2  │  执行器: 7/8 在线        │  │
│  │  总下载速度: 423.5 MB/s  │  总带宽利用率: 68%                        │  │
│  │                                                                       │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                                                              │
│  ┌─ 排队策略配置 ───────────────────────────────────────────────────────┐  │
│  │                                                                       │  │
│  │  并行下载数: [3 ▼]     同时活跃的最大任务数                           │  │
│  │  排队策略:   [优先级 ▼]                                              │  │
│  │             ○ 优先级 (按用户设定的优先级)                             │  │
│  │             ○ 先进先出 (按创建时间)                                   │  │
│  │             ○ 最小优先 (优先下载最小的模型, 快速释放执行器)            │  │
│  │  自动开始:   [☑] 当活跃任务完成, 自动开始排队中的下一个               │  │
│  │                                                                       │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                                                              │
│  ┌─ 任务列表 ───────────────────────────────────────────────────────────┐  │
│  │                                                                       │  │
│  │  #  │ 模型                │ 大小   │ 状态   │ 进度    │ 速度       │  │
│  │ ────┼─────────────────────┼────────┼────────┼─────────┼────────────│  │
│  │  1  │ DeepSeek-V3         │ 689 GB │ ▼ 下载中│ 58.3%  │ 157.3 MB/s │  │
│  │     │ ████████████████░░░░│        │        │         │            │  │
│  │     │ [打开Tab] [⏸暂停] [⬆提升优先级]                              │  │
│  │ ────┼─────────────────────┼────────┼────────┼─────────┼────────────│  │
│  │  2  │ Kimi-K2-Instruct    │ 1.03TB │ ▼ 下载中│ 32.1%  │ 142.8 MB/s │  │
│  │     │ ████████░░░░░░░░░░░░│        │        │         │            │  │
│  │     │ [打开Tab] [⏸暂停] [⬇降低优先级]                              │  │
│  │ ────┼─────────────────────┼────────┼────────┼─────────┼────────────│  │
│  │  3  │ GLM-4-9b-Chat       │ 18.5GB │ ▼ 下载中│ 89.5%  │ 52.1 MB/s  │  │
│  │     │ █████████████████░░░│        │        │         │            │  │
│  │     │ [打开Tab] [⏸暂停]                                             │  │
│  │ ────┼─────────────────────┼────────┼────────┼─────────┼────────────│  │
│  │  4  │ Qwen2.5-72B         │ 145 GB │ 🕐 排队 │ 等待中  │ -          │  │
│  │     │ ░░░░░░░░░░░░░░░░░░░░│ 优先级2 │        │         │            │  │
│  │     │ [打开Tab] [▶立即开始] [⬆提升优先级] [✕取消]                   │  │
│  │ ────┼─────────────────────┼────────┼────────┼─────────┼────────────│  │
│  │  5  │ DeepSeek-R1         │ 689 GB │ ✅ 完成 │ 100%   │ -          │  │
│  │     │ ████████████████████│ 用时1h23m│ 校验通过│         │            │  │
│  │     │ [打开Tab] [📂打开目录] [🗑删除记录]                           │  │
│  │ ────┼─────────────────────┼────────┼────────┼─────────┼────────────│  │
│  │  6  │ Llama-3-8B          │ 16 GB  │ ✅ 完成 │ 100%   │ -          │  │
│  │     │ ████████████████████│ 用时3m45s│ 校验通过│         │            │  │
│  │     │ [打开Tab] [📂打开目录] [🗑删除记录]                           │  │
│  │                                                                       │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                                                              │
│  ┌─ 执行器资源分配视图 ─────────────────────────────────────────────────┐  │
│  │                                                                       │  │
│  │  显示当前执行器资源在各任务间的分配情况:                                │  │
│  │                                                                       │  │
│  │  node-1 ████████ DeepSeek-V3 ████ Kimi-K2                            │  │
│  │  node-2 ████████████ DeepSeek-V3 ██ Kimi-K2                          │  │
│  │  node-3 ████ DeepSeek-V3 ████ GLM-4                                  │  │
│  │  node-4 ████████ Kimi-K2 ████ GLM-4                                  │  │
│  │  node-5 ████████████████ DeepSeek-V3                                 │  │
│  │  node-6 ████████████ Kimi-K2 ██ GLM-4                                │  │
│  │  node-7 ████████████████ DeepSeek-V3                                 │  │
│  │  node-8 (空闲) ← 等待分配                                             │  │
│  │                                                                       │  │
│  │  💡 当 GLM-4 完成(即将)后, node-3/4/6 将自动分配给排队中的 Qwen2.5   │  │
│  │                                                                       │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                                                              │
│  ┌─ 底部全局状态栏 ─────────────────────────────────────────────────────┐  │
│  │                                                                       │  │
│  │  🟢 执行器: 7在线 1空闲 0故障  │  总速度: 352.2 MB/s                  │  │
│  │  💾 存储: 华为云OBS (正常)     │  🕐 排队: 1任务等待                   │  │
│  │                                                                       │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 6.3 任务详情 Tab 页 (点击任务打开)

每个任务打开后进入独立 Tab 页，包含完整的实时监控仪表盘：

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  [📋任务队列]  [🔍DeepSeek-V3 ▼]  [🔍Kimi-K2 ▼]  [🔍GLM-4 ▼]  [+ 新建]   │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  Tab: DeepSeek-V3                                            [⏸暂停] [✕取消]│
│                                                                              │
│  ┌─ 总览卡片 ───────────────────────────────────────────────────────────┐  │
│  │                                                                       │  │
│  │  总进度        下载速度        剩余时间        执行器     存储         │  │
│  │  58.3%        157.3 MB/s     0h 30m 45s      7 在线     华为云OBS    │  │
│  │  401.7/689GB  峰值:210 MB/s  排队:0          0 故障     即时上传     │  │
│  │                                                                       │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                                                              │
│  ┌─ 速度曲线 (实时) ────────────────────────────────────────────────────┐  │
│  │                                                                       │  │
│  │  200 ┤                                          ╭──╮                  │  │
│  │  180 ┤                            ╭──╮    ╭──╯  │                  │  │
│  │  160 ┤               ╭──╮    ╭──╯  ╰──╮╯     │                  │  │
│  │  140 ┤         ╭──╮╯  ╰─╮╯         ╰─╮      │                  │  │
│  │  120 ┤   ╭──╮╯     ╰─╮              ╰─╮     │                  │  │
│  │  100 ┤╯    ╰─╮        ╰─╮              ╰───╯                  │  │
│  │   80 ┤       ╰─╮         ╰─╮                                    │  │
│  │   60 ┤         ╰─╮         ╰─                                   │  │
│  │      └──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬─→ 时间                │  │
│  │        10:00   05   10   15   20   25   30                        │  │
│  │  ── 总速度  ── node-1  ── node-2  ── node-3                       │  │
│  │                                                                       │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                                                              │
│  ┌─ 文件进度矩阵 (全部 163 个文件) ────────────────────────────────────┐  │
│  │                                                                       │  │
│  │  ✅✅✅✅✅✅✅✅✅✅  ← 已完成(下载+上传+校验全通过)                │  │
│  │  ✅✅✅✅✅✅✅✅✅✅                                            │  │
│  │  ✅✅✅✅✅✅✅✅✅✅                                            │  │
│  │  ✅✅✅✅✅✅✅✅✅✅                                            │  │
│  │  ✅✅✅✅✅✅✅✅✅✅                                            │  │
│  │  ✅✅✅✅✅✅✅✅✅✅                                            │  │
│  │  ✅✅✅✅✅✅✅✅✅✅                                            │  │
│  │  ✅✅✅✅✅✅⬛⬛⬛⬛  ← 下载中(蓝色渐变)                          │  │
│  │  ⬛⬛⬛⬛⬛⬛⬛⬛⬛⬛                                            │  │
│  │  🟠🟠⬛⬛⬛⬛⬛⬛⬛⬛  ← 🟠上传中(橙色)                          │  │
│  │  ⬛⬛⬛⬛⬛⬛⬛⬛⬛⬛                                            │  │
│  │  ⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜  ← 等待中(灰色)                             │  │
│  │  ⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜                                            │  │
│  │  ⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜                                            │  │
│  │  ⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜                                            │  │
│  │                                                                       │  │
│  │  ✅ 89 完成+校验  🟠 3 上传中  ⬛ 55 下载中  ⬜ 16 等待  ❌ 0 失败  │  │
│  │  鼠标悬停: 文件名, 大小, 进度%, 执行器, 速度, 校验状态              │  │
│  │                                                                       │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                                                              │
│  ┌─ 文件详情表 (全部文件, 可排序/筛选) ─────────────────────────────────┐  │
│  │  筛选: [全部▼] [下载中▼] [已完成▼] [失败▼]  搜索: [________]        │  │
│  │                                                                       │  │
│  │  文件名                    │大小    │状态    │进度  │下载│本地│远端│  │
│  │                           │        │        │     │校验│校验│校验│  │
│  │  ─────────────────────────┼────────┼────────┼─────┼────┼────┼────│  │
│  │  model-00001-of-163       │ 4.3 GB │ ✅完成 │100% │ ✅ │ ✅ │ ✅ │  │
│  │  model-00002-of-163       │ 4.3 GB │ ✅完成 │100% │ ✅ │ ✅ │ ✅ │  │
│  │  ...                     │        │        │     │    │    │    │  │
│  │  model-00089-of-163       │ 4.3 GB │ ✅完成 │100% │ ✅ │ ✅ │ ✅ │  │
│  │  model-00090-of-163       │ 4.3 GB │ 🟠上传 │100% │ ✅ │ ✅ │ ⏳ │  │
│  │  model-00091-of-163       │ 4.3 GB │ 🟠上传 │100% │ ✅ │ ✅ │ ⏳ │  │
│  │  model-00092-of-163       │ 4.3 GB │ 🟠上传 │100% │ ✅ │ ✅ │ ⏳ │  │
│  │  model-00093-of-163       │ 4.3 GB │ ▼下载中│78%  │ -  │ -  │ -  │  │
│  │    └ node-1  52.4MB/s  ETA:18s  chunks:14/18  threads:6/8          │  │
│  │  model-00094-of-163       │ 4.3 GB │ ▼下载中│45%  │ -  │ -  │ -  │  │
│  │    └ node-3  23.1MB/s  ETA:102s chunks:8/18  threads:4/4           │  │
│  │  model-00095-of-163       │ 4.3 GB │ ▼下载中│12%  │ -  │ -  │ -  │  │
│  │    └ node-2  48.2MB/s  ETA:78s  chunks:2/18   threads:8/8          │  │
│  │  ...                     │        │        │     │    │    │    │  │
│  │  config.json             │ 1.2 KB │ ✅完成 │100% │ ✅ │ ✅ │ ✅ │  │
│  │  tokenizer.json          │ 8.5 MB │ ✅完成 │100% │ ✅ │ ✅ │ ✅ │  │
│  │  model.safetensors.index │ 8.9 MB │ ✅完成 │100% │ ✅ │ ✅ │ ✅ │  │
│  │  generation_config.json  │ 0.2 KB │ ✅完成 │100% │ ✅ │ ✅ │ ✅ │  │
│  │                                                                       │  │
│  │  ── 校验状态说明 ──────────────────────────────────────────────────│  │
│  │  下载校验: 执行器下载完成后本地 SHA256 vs HF LFS SHA256             │  │
│  │  本地校验: 控制器接收/拉取后再次 SHA256 校验                        │  │
│  │  远端校验: 上传到 OBS 后读取远端文件验证 SHA256                     │  │
│  │                                                                       │  │
│  │  ✅ 通过  ⏳ 进行中  ❌ 失败  - 未开始  ⚠ 跳过(无SHA256)           │  │
│  │                                                                       │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                                                              │
│  ┌─ 活跃下载详情 (实时, 自动刷新) ─────────────────────────────────────┐  │
│  │                                                                       │  │
│  │  ┌─ model-00093 (4.3 GB) → node-1 ──────────────────────────────┐   │  │
│  │  │ ████████████████████████████████░░░░░░░░  78.2%               │   │  │
│  │  │ 3.36 GB / 4.30 GB  │  52.4 MB/s  │  ETA: 18s                 │   │  │
│  │  │ Chunk 进度: 14/18  │  6/8 threads active                      │   │  │
│  │  │                                                              │   │  │
│  │  │  chunk_00 [████████████████████] 256MB ✅                     │   │  │
│  │  │  chunk_01 [████████████████████] 256MB ✅                     │   │  │
│  │  │  ...                                                          │   │  │
│  │  │  chunk_14 [████████████████░░░░]  78%  12.3 MB/s ▼           │   │  │
│  │  │  chunk_15 [████████░░░░░░░░░░░░]  32%   8.1 MB/s ▼           │   │  │
│  │  │  chunk_16 [░░░░░░░░░░░░░░░░░░░░]   0%  等待中                │   │  │
│  │  │  chunk_17 [░░░░░░░░░░░░░░░░░░░░]   0%  等待中                │   │  │
│  │  └──────────────────────────────────────────────────────────────┘   │  │
│  │                                                                       │  │
│  │  ┌─ model-00094 (4.3 GB) → node-3 ──────────────────────────────┐   │  │
│  │  │ ████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  45.1%                 │   │  │
│  │  │ 1.94 GB / 4.30 GB  │  23.1 MB/s  │  ETA: 102s                │   │  │
│  │  │ Chunk 进度: 8/18   │  4/4 threads active                      │   │  │
│  │  └──────────────────────────────────────────────────────────────┘   │  │
│  │                                                                       │  │
│  │  ... (共 55 个并行下载, 可展开/折叠, 仅展示前10个, 可搜索)          │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                                                              │
│  ┌─ 执行器分配面板 ─────────────────────────────────────────────────────┐  │
│  │                                                                       │  │
│  │ ┌──────────────────────────────────────────────────────────────────┐ │  │
│  │ │ ID      │ 状态  │健康分│ 速度      │本任务│所有任务│ 心跳 │ 操作 │ │  │
│  │ ├─────────┼──────┼─────┼──────────┼─────┼──────┼─────┼──────│ │  │
│  │ │ node-1  │ 🟢健康│ 95  │89.2 MB/s │ 3文件│ 5文件 │  3s │[详情]│ │  │
│  │ │ node-2  │ 🟢健康│100  │105 MB/s  │ 3文件│ 4文件 │  1s │[详情]│ │  │
│  │ │ node-3  │ 🟡降级│ 55  │23.1 MB/s │ 2文件│ 3文件 │  8s │[详情]│ │  │
│  │ │ node-4  │ 🟡降级│ 62  │35.6 MB/s │ 0文件│ 4文件 │  4s │[详情]│ │  │
│  │ │ node-5  │ 🟢健康│ 88  │78.4 MB/s │ 3文件│ 4文件 │  2s │[详情]│ │  │
│  │ │ node-6  │ 🟢健康│ 91  │82.1 MB/s │ 2文件│ 5文件 │  5s │[详情]│ │  │
│  │ │ node-7  │ 🟢健康│ 87  │75.0 MB/s │ 3文件│ 4文件 │  3s │[详情]│ │  │
│  │ │ node-8  │ 🟢空闲│100  │   -      │ 0文件│ 0文件 │  1s │[详情]│ │  │
│  │ └──────────────────────────────────────────────────────────────────┘ │  │
│  │                                                                       │  │
│  │ 💡 node-4 未分配本任务, 其资源正在服务 Kimi-K2 和 GLM-4              │  │
│  │    GLM-4 完成后 node-4 将自动接管本任务                              │  │
│  │                                                                       │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                                                              │
│  ┌─ 事件日志 (本任务, 实时滚动) ────────────────────────────────────────┐  │
│  │                                                                       │  │
│  │  10:30:45 [INFO]  node-2 完成 model-00051 (4.3GB) 耗时82s           │  │
│  │  10:30:44 [INFO]  model-00051 上传至 华为云OBS 完成 (4.3GB, 78s)    │  │
│  │  10:30:43 [INFO]  model-00051 本地SHA256校验 ✅                      │  │
│  │  10:30:42 [INFO]  model-00051 下载SHA256校验 ✅ 开始上传              │  │
│  │  10:30:43 [INFO]  node-2 开始下载 model-00052 (4.3GB)               │  │
│  │  10:30:40 [WARN]  node-3 速度降至 23 MB/s, 健康分 55, 降级          │  │
│  │  10:30:38 [INFO]  动态调整 node-3: 并发2, 线程4, chunk 64MB         │  │
│  │  10:30:30 [WARN]  node-7 心跳超时, 标记 suspect                      │  │
│  │  10:30:25 [INFO]  重平衡触发: node-3速度偏离                         │  │
│  │  10:30:10 [ERROR] node-7 model-00045 SHA256 不匹配, 重试             │  │
│  │                                                                       │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 6.4 文件校验状态三列设计

每个文件在表格中有三列校验状态，完整追踪从下载到远端存储的全链路：

```
┌────────────────────────────────────────────────────────────────────────────┐
│ 文件                         │大小   │状态   │进度  │下载校验│控制器校验│远端校验│
├──────────────────────────────┼───────┼───────┼─────┼───────┼─────────┼──────┤
│ model-00001-of-163.safetensors│4.3 GB│✅完成 │100% │  ✅    │   ✅    │  ✅  │
│   SHA256: a3f8d2...e9c1 匹配  │      │       │     │ 2.1s  │  1.8s   │ 3.2s │
├──────────────────────────────┼───────┼───────┼─────┼───────┼─────────┼──────┤
│ model-00090-of-163.safetensors│4.3 GB│🟠上传中│100% │  ✅    │   ✅    │  ⏳  │
│   上传: 2.1/4.3GB  52.3 MB/s │      │       │     │ 通过   │  通过   │进行中│
├──────────────────────────────┼───────┼───────┼─────┼───────┼─────────┼──────┤
│ model-00093-of-163.safetensors│4.3 GB│▼下载中│78%  │  -     │   -     │  -   │
│   node-1  52.4 MB/s          │      │       │     │ 未开始 │  未开始  │未开始│
├──────────────────────────────┼───────┼───────┼─────┼───────┼─────────┼──────┤
│ config.json                  │1.2 KB│✅完成 │100% │  ⚠    │   ✅    │  ✅  │
│   无LFS SHA256, 跳过下载校验  │      │       │     │ 跳过  │  大小匹配│校验通过│
├──────────────────────────────┼───────┼───────┼─────┼───────┼─────────┼──────┤
│ model-00045-of-163.safetensors│4.3 GB│❌失败  │100% │  ❌    │   -     │  -   │
│   SHA256不匹配, 重试中(2/5)   │      │       │     │不匹配 │  未开始  │未开始│
│   期望: a3f8d2...e9c1         │      │       │     │       │         │      │
│   实际: 7b2c4f...d8a3         │      │       │     │       │         │      │
└──────────────────────────────┴───────┴───────┴─────┴───────┴─────────┴──────┘

校验状态图标:
  ✅ 通过    — SHA256/大小 完全匹配
  ⏳ 进行中  — 正在校验
  ❌ 失败    — 校验不通过, 已触发重试
  ⚠ 跳过    — 无参考SHA256, 仅做大小校验
  -  未开始  — 前置阶段未完成
```

### 6.5 文件进度矩阵悬停提示

```
鼠标悬停在矩阵中的色块上时显示:

已完成文件:
┌──────────────────────────────────────────────┐
│ model-00001-of-00163.safetensors             │
│ 大小: 4.30 GB                                │
│ 状态: ✅ 全部完成                             │
│                                               │
│ ── 校验链路 ──────────────────────────────  │
│ 下载校验: ✅ SHA256 a3f8d2...e9c1 匹配 (2.1s)│
│ 控制器校验: ✅ SHA256 匹配 (1.8s)            │
│ 远端校验: ✅ SHA256 匹配 (3.2s)              │
│                                               │
│ 存储: obs://ai-model-weights/models/.../      │
│       model-00001-of-00163.safetensors        │
│ 执行器: node-1  │  耗时: 82s                  │
└──────────────────────────────────────────────┘

下载中文件:
┌──────────────────────────────────────────────┐
│ model-00093-of-00163.safetensors             │
│ 大小: 4.30 GB                                │
│ 进度: 78.2% (3.36/4.30 GB)                   │
│ 执行器: node-1 (健康 95分)                    │
│ 速度: 52.4 MB/s  │  Chunks: 14/18            │
│ 已用时间: 65s  │  预计剩余: 18s               │
│ 重试次数: 0                                   │
│ 下载校验: - (未开始)                          │
└──────────────────────────────────────────────┘

上传中文件:
┌──────────────────────────────────────────────┐
│ model-00090-of-00163.safetensors             │
│ 大小: 4.30 GB                                │
│ 状态: 🟠 上传至 华为云OBS 中                  │
│ 上传进度: 2.1/4.3 GB (48.8%)                 │
│ 上传速度: 52.3 MB/s  │  ETA: 42s             │
│ 下载校验: ✅ SHA256 匹配                      │
│ 控制器校验: ✅ SHA256 匹配                    │
│ 远端校验: ⏳ 等待上传完成                     │
└──────────────────────────────────────────────┘
```

### 6.6 节点详情弹窗

```
┌───────────────────────────────────────────────────────────────────┐
│ node-3 详情                                                  [✕] │
├───────────────────────────────────────────────────────────────────┤
│                                                                    │
│  状态: 🟡 降级          健康评分: 55/100                          │
│  主机: gpu-worker-03    IP: 192.168.1.33                          │
│  注册时间: 2026-04-28 08:00:15    运行时长: 2h 30m                │
│                                                                    │
│  ┌─ 性能统计 ──────────────────────────────────────────────────┐  │
│  │  当前速度: 23.1 MB/s   峰值速度: 98.5 MB/s                  │  │
│  │  平均速度: 45.2 MB/s   有效带宽: 23.1 MB/s                  │  │
│  │  任务成功率: 85.7%     连续成功: 1     连续失败: 0           │  │
│  │  已完成: 18 文件       已失败: 3 文件                        │  │
│  └─────────────────────────────────────────────────────────────┘  │
│                                                                    │
│  ┌─ 当前任务分配 ──────────────────────────────────────────────┐  │
│  │  DeepSeek-V3:  model-00090 (45%), model-00091 (12%)        │  │
│  │  Kimi-K2:      model-0020 (33%)                            │  │
│  │  总计: 3 文件活跃                                          │  │
│  └─────────────────────────────────────────────────────────────┘  │
│                                                                    │
│  ┌─ 当前参数 ──────────────────────────────────────────────────┐  │
│  │  最大并发文件: 2 (自动调整, 原3)                             │  │
│  │  每文件线程数: 4 (自动调整, 原8)                             │  │
│  │  Chunk大小:   64MB (自动调整, 原256MB)                      │  │
│  └─────────────────────────────────────────────────────────────┘  │
│                                                                    │
│  ┌─ 速度曲线(最近1小时) ──────────────────────────────────────┐  │
│  │  100┤╮                                                       │  │
│  │   80┤╰──╮                                                    │  │
│  │   60┤   ╰──╮                                                 │  │
│  │   40┤      ╰──╮                                              │  │
│  │   20┤         ╰───╮     ╭─                                   │  │
│  │    0┤              ╰───╯                                     │  │
│  │     └──────────────────────────────────→                     │  │
│  │   速度在 30m 前开始下降, 疑似网络波动                          │  │
│  └─────────────────────────────────────────────────────────────┘  │
│                                                                    │
│  ┌─ 故障历史(最近) ────────────────────────────────────────────┐  │
│  │  10:25  SHA256 不匹配 model-00088, 自动重试成功              │  │
│  │  10:15  连接超时 model-00085, 换节点重试                      │  │
│  │  10:10  HTTP 429 model-00083, 退避重试成功                   │  │
│  └─────────────────────────────────────────────────────────────┘  │
│                                                                    │
│  [暂停该节点任务]  [隔离该节点]  [重置健康评分]                      │
│                                                                    │
└───────────────────────────────────────────────────────────────────┘
```

---

## 7. 全速下载优化策略

### 7.1 全速下载控制器

```python
class FullSpeedController:
    """
    目标: 最大化利用所有可用带宽, 以最快速度完成全部下载

    核心策略:
    1. 初始探测: 启动时小文件测速, 建立每个节点的带宽基线
    2. 参数自适应: 根据实时速度动态调整并发/线程/chunk
    3. 饥饿检测: 空闲节点立即分配任务
    4. 尾部加速: 最后几个文件集中所有节点并行下载
    5. 限流感知: 检测到 429 自动降速, 避免被长期封禁
    """

    def __init__(self, node_manager: NodeStateManager,
                 load_balancer: LoadBalanceEngine):
        self.node_manager = node_manager
        self.load_balancer = load_balancer
        self._rate_limit_detected = False

    def on_speed_update(self, executor_id: str, speed_bps: float):
        profile = self.node_manager.get_profile(executor_id)
        old_speed = profile.avg_speed_bps

        # 更新速度
        profile.speed_samples.append(SpeedSample(
            timestamp=datetime.utcnow(), speed_bps=speed_bps,
            active_files=profile.max_concurrent_files,
            active_threads=profile.chunk_threads_per_file,
        ))
        profile.avg_speed_bps = statistics.mean(
            s.speed_bps for s in list(profile.speed_samples)[-20:]
        )

        # 限流检测
        if speed_bps == 0 and old_speed > 0:
            if not self._rate_limit_detected:
                self._rate_limit_detected = True
                self._handle_rate_limit(executor_id)

        # 带宽未充分利用 → 加大参数
        if speed_bps < profile.effective_bandwidth * 0.5:
            self._increase_parallelism(profile)

        # 带宽饱和 → 不变
        # 带宽超载(丢包/重试增加) → 降低参数
        if profile.consecutive_failures > 0:
            self._decrease_parallelism(profile)

    def _handle_rate_limit(self, executor_id: str):
        """检测到限流: 降低该节点所有参数"""
        profile = self.node_manager.get_profile(executor_id)
        profile.max_concurrent_files = max(1, profile.max_concurrent_files - 1)
        profile.chunk_threads_per_file = max(2, profile.chunk_threads_per_file - 2)
        log.warning(f"Rate limit detected on {executor_id}, "
                    f"reducing to {profile.max_concurrent_files} files, "
                    f"{profile.chunk_threads_per_file} threads")

    def _increase_parallelism(self, profile: NodeHealthProfile):
        if profile.chunk_threads_per_file < 16:
            profile.chunk_threads_per_file += 2
            log.info(f"Increasing threads for {profile.executor_id} "
                     f"to {profile.chunk_threads_per_file}")
        if profile.max_concurrent_files < 5:
            profile.max_concurrent_files += 1

    def _decrease_parallelism(self, profile: NodeHealthProfile):
        if profile.chunk_threads_per_file > 2:
            profile.chunk_threads_per_file -= 2
        if profile.max_concurrent_files > 1:
            profile.max_concurrent_files -= 1

    def on_task_nearing_completion(self, task: DownloadTask):
        """尾部加速: 最后 10% 文件集中力量"""
        remaining = [s for s in task.subtasks if s.status != "completed"]
        total = len(task.subtasks)
        if len(remaining) <= total * 0.1:
            # 所有节点全力下载剩余文件
            for profile in self.node_manager.get_all_profiles():
                if profile.can_accept_tasks:
                    profile.max_concurrent_files += 2
                    profile.chunk_threads_per_file = min(
                        16, profile.chunk_threads_per_file + 4
                    )
                    self._push_param_update(profile)
            log.info(f"TAIL BOOST: {len(remaining)} files remaining, "
                     f"all executors boosted")
```

### 7.2 自适应参数映射表

```
实时速度(MB/s) → 推荐参数配置

┌───────────────┬───────────┬──────────┬──────────┬──────────────┐
│ 速度范围       │ 并发文件数 │ 每文件线程│ Chunk大小│ 适用场景      │
├───────────────┼───────────┼──────────┼──────────┼──────────────┤
│ > 100         │ 5         │ 12-16    │ 512 MB   │ 高速专线      │
│ 50 - 100      │ 4         │ 8-12     │ 256 MB   │ 千兆网络      │
│ 20 - 50       │ 3         │ 6-8      │ 128 MB   │ 百兆网络      │
│ 5 - 20        │ 2         │ 4        │ 64 MB    │ 弱网环境      │
│ < 5           │ 1         │ 2        │ 32 MB    │ 极慢网络/限流 │
│ 0 (限流)      │ 1         │ 1        │ 16 MB    │ 等待恢复      │
└───────────────┴───────────┴──────────┴──────────┴──────────────┘

调整频率: 每 30 秒评估一次, 避免频繁切换
调整阈值: 速度变化超过 30% 才触发调整
```

### 7.3 全局调度决策流程

```
每 10 秒执行一次全局调度评估:

1. 收集所有执行器最新状态
2. 计算总聚合速度 = Σ(executor.avg_speed)
3. 计算剩余字节 = Σ(subtask.file_size - subtask.downloaded_bytes) for 未完成
4. 计算 ETA = 剩余字节 / 总聚合速度

5. 检查是否需要重平衡:
   a. 有空闲执行器(无活跃任务)? → 立即分配 pending 文件
   b. 有执行器完成文件? → 立即补充 pending 文件
   c. 有执行器速度偏离均值 >30%? → 调整该执行器参数
   d. 有执行器 3 次心跳未上报? → 标记 suspect, 准备回收任务
   e. 有 fault 节点探测恢复? → 重新分配任务
   f. 剩余文件 < 10%? → 启动尾部加速模式

6. 下发参数调整指令给需要调整的执行器
7. 通过 WebSocket 推送最新状态给 UI
```

---

## 8. 存储后端配置与自动装配

### 8.1 支持的存储后端

| 存储类型 | 协议 | 说明 |
|---------|------|------|
| **华为云 OBS** | S3 兼容 | obs.cn-xxx.myhuaweicloud.com |
| **阿里云 OSS** | S3 兼容 | oss-cn-xxx.aliyuncs.com |
| **腾讯云 COS** | S3 兼容 | cos.ap-xxx.myqcloud.com |
| **AWS S3** | 原生 S3 | s3.amazonaws.com |
| **MinIO** | S3 兼容 | 自部署 |
| **本地磁盘** | file:// | 控制器本机或 NFS 挂载 |
| **SSH/rsync** | rsync | 远程服务器 |

> 所有 S3 兼容存储统一使用 boto3 SDK，只需配置 endpoint 即可适配不同厂商。

### 8.2 UI 存储配置页面

```
┌─────────────────────────────────────────────────────────────────────┐
│  [搜索页]  [下载任务]  [执行器管理]  [存储配置]  [系统设置]          │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  ┌─ 已配置的存储后端 ─────────────────────────────────────────────┐ │
│  │                                                               │ │
│  │  ┌──────────────────────────────────────────────────────────┐ │ │
│  │  │ 🟢 华为云OBS-北京                    [编辑] [测试] [删除] │ │ │
│  │  │ 类型: 华为云 OBS                                         │ │ │
│  │  │ Endpoint: obs.cn-north-4.myhuaweicloud.com               │ │ │
│  │  │ Bucket: ai-model-weights                                 │ │ │
│  │  │ 根路径: /models/                                         │ │ │
│  │  │ 区域: cn-north-4                                         │ │ │
│  │  │ AK: ****************************xYz3                     │ │ │
│  │  │ SK: •••••••••••••••••••••••••••••                        │ │ │
│  │  │ 上传线程: 8  │  分片大小: 128MB                           │ │ │
│  │  │ 上传带宽限制: 不限                                        │ │ │
│  │  │ 最后测试: 2026-04-28 10:00 ✅ 连接正常, 写入/读取/删除 通过│ │ │
│  │  └──────────────────────────────────────────────────────────┘ │ │
│  │                                                               │ │
│  │  ┌──────────────────────────────────────────────────────────┐ │ │
│  │  │ 🟢 本地NAS存储                       [编辑] [测试] [删除] │ │ │
│  │  │ 类型: 本地磁盘                                            │ │ │
│  │  │ 路径: /mnt/nas/model-weights/                             │ │ │
│  │  │ 可用空间: 8.5 TB                                         │ │ │
│  │  └──────────────────────────────────────────────────────────┘ │ │
│  │                                                               │ │
│  │  ┌──────────────────────────────────────────────────────────┐ │ │
│  │  │ 🟡 MinIO-测试集群                   [编辑] [测试] [删除] │ │ │
│  │  │ 类型: MinIO                                               │ │ │
│  │  │ Endpoint: minio.test.local:9000                           │ │ │
│  │  │ Bucket: hf-models                                        │ │ │
│  │  │ 最后测试: 2026-04-27 15:30 ⚠ 连接超时                    │ │ │
│  │  └──────────────────────────────────────────────────────────┘ │ │
│  │                                                               │ │
│  │  [+ 添加存储后端]                                             │ │
│  └───────────────────────────────────────────────────────────────┘  │
│                                                                      │
│  ┌─ 添加/编辑存储后端弹窗 ───────────────────────────────────────┐ │
│  │                                                               │ │
│  │  名称: [华为云OBS-北京___________]                           │ │
│  │                                                               │ │
│  │  存储类型: [华为云 OBS ▼]                                     │ │
│  │  ┌───────────────────────────────────────────────────────┐   │ │
│  │  │ ○ 华为云 OBS    ○ 阿里云 OSS    ○ 腾讯云 COS          │   │ │
│  │  │ ○ AWS S3        ○ MinIO         ○ 本地磁盘             │   │ │
│  │  │ ○ 自定义 S3 兼容存储                                   │   │ │
│  │  └───────────────────────────────────────────────────────┘   │ │
│  │                                                               │ │
│  │  ── 连接配置 ──────────────────────────────────────────────  │ │
│  │  Endpoint*: [obs.cn-north-4.myhuaweicloud.com____________]   │ │
│  │  Region:    [cn-north-4__________________________________]   │ │
│  │  Bucket*:   [ai-model-weights____________________________]   │ │
│  │  AK/Access Key: [*****************************************] │ │
│  │  SK/Secret Key: [*****************************************] │ │
│  │                                                               │ │
│  │  ── 路径配置 ──────────────────────────────────────────────  │ │
│  │  根路径:    [/models/____________________________________]   │ │
│  │                                                               │ │
│  │  路径模板(可自定义):                                          │ │
│  │  [/models/{org}/{model_name}/{revision}/]                    │ │
│  │  示例: /models/deepseek-ai/DeepSeek-V3/main/                 │ │
│  │  可用变量: {org} {model_name} {revision} {date} {task_id}    │ │
│  │                                                               │ │
│  │  ── 上传配置 ──────────────────────────────────────────────  │ │
│  │  上传并发线程: [8▼]                                           │ │
│  │  分片上传大小: [128 MB▼]     (S3 multipart upload)           │ │
│  │  上传带宽限制: [不限 ▼]      (MB/s, 0=不限)                  │ │
│  │  上传失败重试: [3] 次                                         │ │
│  │  服务端加密:   [☐ 启用 SSE-S3]                               │ │
│  │                                                               │ │
│  │  ── 存储生命周期 ──────────────────────────────────────────  │ │
│  │  已存在文件策略: [跳过 ▼]                                     │ │
│  │  ┌───────────────────────────────────────────────────────┐   │ │
│  │  │ ○ 跳过 (推荐, 文件已存在则不重复上传)                    │   │ │
│  │  │ ○ 覆盖 (始终重新上传)                                    │   │ │
│  │  │ ○ 校验后决定 (对比 SHA256, 不同才上传)                   │   │ │
│  │  │ ○ 备份后覆盖 (将旧文件移到 .bak/)                       │   │ │
│  │  └───────────────────────────────────────────────────────┘   │ │
│  │                                                               │ │
│  │  临时文件清理: [☑ 上传成功后删除执行器本地临时文件]           │ │
│  │                                                               │ │
│  │                        [取消]  [测试连接]  [保存]             │ │
│  │                                                               │ │
│  │  测试连接结果:                                                │ │
│  │  ✅ 连接成功 (延迟 23ms)                                     │ │
│  │  ✅ Bucket 存在且可写                                        │ │
│  │  ✅ 写入测试文件: 1.2 KB OK                                  │ │
│  │  ✅ 读取测试文件: 校验通过                                    │ │
│  │  ✅ 删除测试文件: OK                                         │ │
│  │  ✅ 可用空间: 无限 (对象存储)                                 │ │
│  │                                                               │ │
│  └───────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

### 8.3 创建下载任务时的存储选择

```
┌───────────────────────────────────────────────────────────────────┐
│  创建下载任务                                                 [✕] │
├───────────────────────────────────────────────────────────────────┤
│                                                                    │
│  ── 模型信息 ──────────────────────────────────────────────────── │
│  仓库: deepseek-ai/DeepSeek-V3                                    │
│  分支/版本: [main ▼]                                              │
│  Token:    [hf_xxxxxxxxxxxxxx        ]  (受限模型需要)            │
│                                                                    │
│  ── 文件筛选 ──────────────────────────────────────────────────── │
│  ☑ 全部选择 (163 文件, 689 GB)                                    │
│  ☐ 仅权重文件 (*.safetensors)                                      │
│  ☐ 自定义筛选...                                                   │
│                                                                    │
│  ── 目标存储配置 ──────────────────────────────────────────────── │
│                                                                    │
│  存储后端: [华为云OBS-北京 ▼]         ← 从存储配置页选择的已配置后端│
│                                                                    │
│  上传路径: (根据存储配置的路径模板自动生成, 可手动修改)             │
│  [/models/deepseek-ai/DeepSeek-V3/main/]                          │
│                                                                    │
│  ┌─ 预览文件分布 ──────────────────────────────────────────────┐  │
│  │                                                              │  │
│  │  obs://ai-model-weights/models/deepseek-ai/DeepSeek-V3/main/ │  │
│  │  ├── model-00001-of-00163.safetensors  (4.30 GB)             │  │
│  │  ├── model-00002-of-00163.safetensors  (4.30 GB)             │  │
│  │  ├── ...                                                     │  │
│  │  ├── model-00163-of-00163.safetensors  (3.12 GB)             │  │
│  │  ├── model.safetensors.index.json     (8.90 MB)              │  │
│  │  ├── config.json                       (1.20 KB)              │  │
│  │  ├── tokenizer.json                    (8.50 MB)              │  │
│  │  ├── generation_config.json            (0.20 KB)              │  │
│  │  └── ...                                                     │  │
│  │                                                              │  │
│  │  共 163 文件  │  总计 689 GB  │  预计存储费用: ~¥xxx/月       │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                    │
│  ── 装配策略 ──────────────────────────────────────────────────── │
│                                                                    │
│  装配模式: [● 即时上传]  ○ 延迟批量上传  ○ 仅下载不上传           │
│                                                                    │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ ● 即时上传 (推荐):                                          │  │
│  │   每个文件下载完成+校验通过后立即上传到存储后端               │  │
│  │   优点: 实时可见结果, 不占用本地空间                          │  │
│  │                                                              │  │
│  │ ○ 延迟批量上传:                                              │  │
│  │   所有文件下载完成后, 统一批量上传                            │  │
│  │   优点: 可做最终一致性校验后再上传                            │  │
│  │   缺点: 需要足够本地临时存储空间                              │  │
│  │                                                              │  │
│  │ ○ 仅下载不上传:                                              │  │
│  │   仅下载到执行器本地, 手动后续处理                            │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                    │
│  已存在文件: [校验后决定 ▼]                                        │
│                                                                    │
│  ☑ 上传成功后自动清理执行器本地临时文件                            │
│  ☑ 上传完成后进行服务端校验 (读取远端文件验证 SHA256)              │
│                                                                    │
│  ── 下载选项 ──────────────────────────────────────────────────── │
│                                                                    │
│  下载优先执行器: [自动选择 ▼] / [指定执行器...]                    │
│  单文件最大线程: [自动 ▼]                                          │
│                                                                    │
│                                             [取消]  [开始下载]     │
│                                                                    │
└───────────────────────────────────────────────────────────────────┘
```

### 8.4 存储后端抽象层实现

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum


class StorageType(str, Enum):
    HUAWEI_OBS = "huawei_obs"
    ALIYUN_OSS = "aliyun_oss"
    TENCENT_COS = "tencent_cos"
    AWS_S3 = "aws_s3"
    MINIO = "minio"
    LOCAL = "local"
    CUSTOM_S3 = "custom_s3"


class ExistsPolicy(str, Enum):
    SKIP = "skip"
    OVERWRITE = "overwrite"
    VERIFY_THEN_DECIDE = "verify_then_decide"
    BACKUP_THEN_OVERWRITE = "backup_then_overwrite"


class UploadMode(str, Enum):
    IMMEDIATE = "immediate"
    BATCH = "batch"
    DOWNLOAD_ONLY = "download_only"


@dataclass
class StorageConfig:
    id: str
    name: str
    storage_type: StorageType
    endpoint: str | None = None
    region: str | None = None
    bucket: str | None = None
    access_key: str | None = None
    secret_key: str | None = None
    root_path: str = "/"
    path_template: str = "/models/{org}/{model_name}/{revision}/"
    upload_threads: int = 8
    multipart_threshold: int = 64 * 1024 * 1024       # 64MB 以上使用分片上传
    multipart_chunk_size: int = 128 * 1024 * 1024      # 128MB 分片
    bandwidth_limit_mbps: float = 0                     # 0=不限
    upload_retries: int = 3
    server_side_encryption: bool = False
    exists_policy: ExistsPolicy = ExistsPolicy.VERIFY_THEN_DECIDE
    upload_mode: UploadMode = UploadMode.IMMEDIATE
    cleanup_after_upload: bool = True
    verify_after_upload: bool = True


@dataclass
class UploadResult:
    success: bool
    remote_path: str
    file_size: int
    upload_time_seconds: float
    sha256_verified: bool = False
    error: str | None = None


class StorageBackend(ABC):
    @abstractmethod
    def test_connection(self) -> ConnectionTestResult: ...

    @abstractmethod
    def upload_file(self, local_path: str, remote_path: str,
                    expected_sha256: str = None) -> UploadResult: ...

    @abstractmethod
    def download_file(self, remote_path: str, local_path: str) -> str: ...

    @abstractmethod
    def exists(self, remote_path: str) -> bool: ...

    @abstractmethod
    def get_metadata(self, remote_path: str) -> FileMetadata: ...

    @abstractmethod
    def delete(self, remote_path: str) -> bool: ...

    @abstractmethod
    def list_files(self, prefix: str) -> list[FileMetadata]: ...


class S3CompatibleStorage(StorageBackend):
    """统一实现: 华为OBS / 阿里OSS / 腾讯COS / AWS S3 / MinIO / 自定义S3"""

    def __init__(self, config: StorageConfig):
        self.config = config
        self.client = self._create_client()

    def _create_client(self):
        import boto3
        from botocore.config import Config as BotoConfig

        endpoint_map = {
            StorageType.HUAWEI_OBS: f"obs.{self.config.region}.myhuaweicloud.com",
            StorageType.ALIYUN_OSS: f"oss-cn-{self.config.region}.aliyuncs.com",
            StorageType.TENCENT_COS: f"cos.{self.config.region}.myqcloud.com",
            StorageType.MINIO: self.config.endpoint,
            StorageType.CUSTOM_S3: self.config.endpoint,
        }

        endpoint_url = self.config.endpoint or f"https://{endpoint_map.get(self.config.storage_type, '')}"

        boto_config = BotoConfig(
            max_pool_connections=self.config.upload_threads * 2,
            retries={"max_attempts": self.config.upload_retries, "mode": "adaptive"},
        )

        return boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=self.config.region or "",
            aws_access_key_id=self.config.access_key,
            aws_secret_access_key=self.config.secret_key,
            config=boto_config,
        )

    def test_connection(self) -> ConnectionTestResult:
        checks = []
        try:
            self.client.head_bucket(Bucket=self.config.bucket)
            checks.append(("Bucket 存在", True, ""))
        except Exception as e:
            checks.append(("Bucket 存在", False, str(e)))
            return ConnectionTestResult(success=False, checks=checks)

        test_key = f"{self.config.root_path}.connection_test_{uuid4().hex[:8]}"
        test_data = f"test_{datetime.utcnow().isoformat()}".encode()

        try:
            self.client.put_object(Bucket=self.config.bucket, Key=test_key, Body=test_data)
            checks.append(("写入测试", True, ""))
        except Exception as e:
            checks.append(("写入测试", False, str(e)))
            return ConnectionTestResult(success=False, checks=checks)

        try:
            resp = self.client.get_object(Bucket=self.config.bucket, Key=test_key)
            content = resp["Body"].read()
            checks.append(("读取测试", content == test_data, ""))
        except Exception as e:
            checks.append(("读取测试", False, str(e)))

        try:
            self.client.delete_object(Bucket=self.config.bucket, Key=test_key)
            checks.append(("删除测试", True, ""))
        except Exception as e:
            checks.append(("删除测试", False, str(e)))

        return ConnectionTestResult(success=all(c[1] for c in checks), checks=checks)

    def upload_file(self, local_path: str, remote_path: str,
                    expected_sha256: str = None) -> UploadResult:
        full_key = self._resolve_key(remote_path)
        file_size = os.path.getsize(local_path)
        start = time.time()

        try:
            if self.config.exists_policy != ExistsPolicy.OVERWRITE:
                if self._key_exists(full_key):
                    if self.config.exists_policy == ExistsPolicy.SKIP:
                        return UploadResult(True, full_key, file_size, 0, False, "skipped_existing")
                    elif self.config.exists_policy == ExistsPolicy.VERIFY_THEN_DECIDE:
                        remote_meta = self.get_metadata(remote_path)
                        if remote_meta and remote_meta.size == file_size:
                            if expected_sha256 and remote_meta.etag:
                                return UploadResult(True, full_key, file_size, 0, False, "skipped_matched")
                            return UploadResult(True, full_key, file_size, 0, False, "skipped_size_match")
                    elif self.config.exists_policy == ExistsPolicy.BACKUP_THEN_OVERWRITE:
                        self.client.copy_object(
                            Bucket=self.config.bucket,
                            CopySource={"Bucket": self.config.bucket, "Key": full_key},
                            Key=full_key + ".bak",
                        )

            extra_args = {}
            if self.config.server_side_encryption:
                extra_args["ServerSideEncryption"] = "AES256"
            if expected_sha256:
                extra_args["Metadata"] = {"sha256": expected_sha256}

            if file_size > self.config.multipart_threshold:
                self._multipart_upload(local_path, full_key, file_size, extra_args)
            else:
                self.client.upload_file(
                    local_path, self.config.bucket, full_key,
                    ExtraArgs=extra_args if extra_args else None,
                )

            elapsed = time.time() - start

            if self.config.verify_after_upload and expected_sha256:
                verified = self._verify_remote(full_key, expected_sha256, file_size)
            else:
                verified = False

            return UploadResult(
                success=True, remote_path=full_key,
                file_size=file_size, upload_time_seconds=elapsed,
                sha256_verified=verified,
            )
        except Exception as e:
            return UploadResult(False, full_key, file_size, time.time() - start, False, str(e))

    def _multipart_upload(self, local_path: str, key: str,
                          file_size: int, extra_args: dict):
        from boto3.s3.transfer import TransferConfig

        transfer_config = TransferConfig(
            multipart_threshold=self.config.multipart_threshold,
            multipart_chunksize=self.config.multipart_chunk_size,
            max_concurrency=self.config.upload_threads,
            num_download_attempts=self.config.upload_retries,
        )
        self.client.upload_file(
            local_path, self.config.bucket, key,
            ExtraArgs=extra_args if extra_args else None,
            Config=transfer_config,
        )

    def _verify_remote(self, key: str, expected_sha256: str, expected_size: int) -> bool:
        resp = self.client.head_object(Bucket=self.config.bucket, Key=key)
        actual_size = resp["ContentLength"]
        if actual_size != expected_size:
            return False
        return True

    def _resolve_key(self, remote_path: str) -> str:
        root = self.config.root_path.strip("/")
        path = remote_path.strip("/")
        return f"{root}/{path}" if root else path

    def _key_exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.config.bucket, Key=key)
            return True
        except self.client.exceptions.NoSuchKey:
            return False
        except Exception:
            return False

    def exists(self, remote_path: str) -> bool:
        return self._key_exists(self._resolve_key(remote_path))

    def get_metadata(self, remote_path: str) -> FileMetadata | None:
        key = self._resolve_key(remote_path)
        try:
            resp = self.client.head_object(Bucket=self.config.bucket, Key=key)
            return FileMetadata(
                key=key, size=resp["ContentLength"],
                etag=resp.get("ETag", "").strip('"'),
                last_modified=resp.get("LastModified"),
                metadata=resp.get("Metadata", {}),
            )
        except Exception:
            return None

    def download_file(self, remote_path: str, local_path: str) -> str:
        key = self._resolve_key(remote_path)
        self.client.download_file(self.config.bucket, key, local_path)
        return local_path

    def delete(self, remote_path: str) -> bool:
        key = self._resolve_key(remote_path)
        self.client.delete_object(Bucket=self.config.bucket, Key=key)
        return True

    def list_files(self, prefix: str) -> list[FileMetadata]:
        key_prefix = self._resolve_key(prefix)
        results = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.config.bucket, Prefix=key_prefix):
            for obj in page.get("Contents", []):
                results.append(FileMetadata(
                    key=obj["Key"], size=obj["Size"],
                    etag=obj.get("ETag", "").strip('"'),
                    last_modified=obj.get("LastModified"),
                ))
        return results


class LocalStorage(StorageBackend):
    def __init__(self, config: StorageConfig):
        self.root = config.root_path
        self.config = config

    def upload_file(self, local_path: str, remote_path: str,
                    expected_sha256: str = None) -> UploadResult:
        target = os.path.join(self.root, remote_path.lstrip("/"))
        os.makedirs(os.path.dirname(target), exist_ok=True)

        if os.path.exists(target) and self.config.exists_policy == ExistsPolicy.SKIP:
            if os.path.getsize(target) == os.path.getsize(local_path):
                return UploadResult(True, target, os.path.getsize(local_path), 0)

        start = time.time()
        shutil.copy2(local_path, target)
        elapsed = time.time() - start
        return UploadResult(True, target, os.path.getsize(local_path), elapsed)

    def test_connection(self) -> ConnectionTestResult:
        checks = []
        exists = os.path.exists(self.root)
        checks.append(("目录存在", exists, ""))
        if exists:
            writable = os.access(self.root, os.W_OK)
            checks.append(("可写", writable, ""))
            if writable:
                usage = shutil.disk_usage(self.root)
                checks.append((f"可用空间: {usage.free / 1e12:.1f} TB", True, ""))
        return ConnectionTestResult(success=all(c[1] for c in checks), checks=checks)

    def exists(self, remote_path: str) -> bool:
        return os.path.exists(os.path.join(self.root, remote_path.lstrip("/")))

    def get_metadata(self, remote_path: str) -> FileMetadata | None:
        path = os.path.join(self.root, remote_path.lstrip("/"))
        if not os.path.exists(path):
            return None
        st = os.stat(path)
        return FileMetadata(key=path, size=st.st_size, last_modified=datetime.fromtimestamp(st.st_mtime))

    def download_file(self, remote_path: str, local_path: str) -> str:
        shutil.copy2(os.path.join(self.root, remote_path.lstrip("/")), local_path)
        return local_path

    def delete(self, remote_path: str) -> bool:
        path = os.path.join(self.root, remote_path.lstrip("/"))
        os.remove(path)
        return True

    def list_files(self, prefix: str) -> list[FileMetadata]:
        results = []
        root = os.path.join(self.root, prefix.lstrip("/"))
        for dirpath, _, filenames in os.walk(root):
            for fn in filenames:
                fp = os.path.join(dirpath, fn)
                st = os.stat(fp)
                results.append(FileMetadata(key=fp, size=st.st_size, last_modified=datetime.fromtimestamp(st.st_mtime)))
        return results
```

### 8.5 存储装配管线

```python
@dataclass
class FileMetadata:
    key: str
    size: int
    etag: str | None = None
    last_modified: datetime | None = None
    metadata: dict | None = None


@dataclass
class ConnectionTestResult:
    success: bool
    checks: list[tuple[str, bool, str]]


class StorageAssemblyPipeline:
    """
    装配管线: 下载完成 → 校验 → 上传存储 → 远端校验 → 清理

    即时模式: 每个文件完成后立即上传
    批量模式: 所有文件下载完后统一上传
    """

    def __init__(self, storage_backend: StorageBackend, config: StorageConfig):
        self.backend = storage_backend
        self.config = config
        self._upload_queue: asyncio.Queue | None = None
        self._upload_workers: list[asyncio.Task] = []

    async def start(self):
        if self.config.upload_mode == UploadMode.IMMEDIATE:
            self._upload_queue = asyncio.Queue()
            for i in range(self.config.upload_threads):
                task = asyncio.create_task(self._upload_worker(i))
                self._upload_workers.append(task)

    async def on_file_downloaded(self, subtask: FileSubTask, local_path: str,
                                 expected_sha256: str):
        if self.config.upload_mode == UploadMode.DOWNLOAD_ONLY:
            subtask.status = "completed"
            return

        if self.config.upload_mode == UploadMode.IMMEDIATE:
            await self._upload_queue.put((subtask, local_path, expected_sha256))
            subtask.status = "uploading"
        elif self.config.upload_mode == UploadMode.BATCH:
            subtask.status = "ready_to_upload"

    async def batch_upload_all(self, task: DownloadTask):
        if self.config.upload_mode != UploadMode.BATCH:
            return
        ready = [s for s in task.subtasks if s.status == "ready_to_upload"]
        for subtask in ready:
            await self._upload_queue.put((
                subtask, subtask.local_path, subtask.expected_sha256
            ))
            subtask.status = "uploading"

    async def _upload_worker(self, worker_id: int):
        while True:
            subtask, local_path, sha256 = await self._upload_queue.get()
            try:
                remote_path = self._build_remote_path(subtask)
                result = await asyncio.to_thread(
                    self.backend.upload_file, local_path, remote_path, sha256
                )

                if result.success:
                    subtask.status = "upload_completed"
                    subtask.upload_result = result
                    if self.config.cleanup_after_upload:
                        os.remove(local_path)

                    if result.sha256_verified:
                        subtask.status = "completed"
                    else:
                        subtask.status = "uploaded_unverified"
                else:
                    subtask.status = "upload_failed"
                    subtask.last_error = result.error
            except Exception as e:
                subtask.status = "upload_failed"
                subtask.last_error = str(e)
            finally:
                self._upload_queue.task_done()

    def _build_remote_path(self, subtask: FileSubTask) -> str:
        task = subtask.task
        parts = task.repo_id.split("/")
        org = parts[0] if len(parts) > 1 else ""
        model_name = parts[1] if len(parts) > 1 else parts[0]

        path = self.config.path_template.format(
            org=org,
            model_name=model_name,
            revision=task.revision or "main",
            date=datetime.utcnow().strftime("%Y%m%d"),
            task_id=str(task.id),
        )
        return f"{path}/{subtask.filename}".replace("//", "/")
```

### 8.6 更新后的任务状态机(增加上传阶段)

```
                     ┌─────────┐
              创建    │         │
            ────────>│ pending │ <────── 重试
                     │         │
                     └────┬────┘
                          │
                          ▼
                     ┌──────────┐
                     │ assigned │
                     └────┬─────┘
                          │
                          ▼
                  ┌──────────────┐
                  │ downloading  │
                  └──────┬───────┘
                         │ 下载完成 + 本地 SHA256 校验
                         ▼
                  ┌──────────────┐
                  │  verified    │
                  │ (本地校验通过) │
                  └──────┬───────┘
                         │
            ┌────────────┼────────────┐
            │ 即时上传    │ 批量上传    │ 仅下载
            ▼            │            ▼
     ┌─────────────┐     │     ┌──────────┐
     │  uploading  │     │     │completed │
     │ (上传到存储) │     │     └──────────┘
     └──────┬──────┘     │
            │            │
            ▼            │
     ┌─────────────┐     │
     │upload_verify│     │
     │(远端校验)    │     │
     └──────┬──────┘     │
            │            │
            ▼            ▼
     ┌─────────────┐
     │  completed  │
     └─────────────┘
```

### 8.7 更新后的进度汇报(含上传阶段)

```json
{
  "subtask_id": "st-001",
  "filename": "model-00001-of-00163.safetensors",
  "status": "uploading",
  "download": {
    "downloaded_bytes": 4592318464,
    "file_size": 4592318464,
    "speed_bps": 0,
    "duration_seconds": 82,
    "sha256_verified": true
  },
  "upload": {
    "uploaded_bytes": 3221225472,
    "total_bytes": 4592318464,
    "speed_bps": 78643200,
    "progress_percent": 70.1,
    "storage_backend": "华为云OBS-北京",
    "remote_path": "obs://ai-model-weights/models/deepseek-ai/DeepSeek-V3/main/model-00001-of-00163.safetensors"
  }
}
```

### 8.8 更新后的文件进度矩阵(含上传状态)

```
文件进度矩阵颜色编码:

✅ 绿色   — 全部完成(下载+上传+校验)
🔵 蓝色渐变 — 下载中
🟡 黄色   — 已下载, 等待上传 / 上传中
🟠 橙色   — 上传中
🟣 紫色   — 远端校验中
⬜ 灰色   — 等待分配
❌ 红色   — 失败

矩阵示例:
✅✅✅✅✅✅✅✅✅✅
✅✅✅✅✅✅✅✅✅✅
✅✅✅✅✅✅✅✅🟠🟠   ← 🟠正在上传
🔵🔵🔵🔵⬛⬛⬛⬛⬛⬛   ← 🔵正在下载, ⬛已分配待开始
⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜

统计栏:
✅ 95 完成(含上传)  🔵 30 下载中  🟠 5 上传中  ⬜ 33 等待
```

### 8.9 存储配置 API

```
GET    /api/storage                          列出所有存储配置
POST   /api/storage                          添加存储配置
PUT    /api/storage/{storage_id}             更新存储配置
DELETE /api/storage/{storage_id}             删除存储配置
POST   /api/storage/{storage_id}/test        测试连接
GET    /api/storage/{storage_id}/browse?prefix=xxx  浏览远端文件
GET    /api/storage/{storage_id}/usage       查询存储用量

POST /api/tasks  的 Body 扩展:
{
  "repo_id": "deepseek-ai/DeepSeek-V3",
  "revision": "main",
  "storage_id": "storage-huawei-obs-01",
  "upload_mode": "immediate",
  "exists_policy": "verify_then_decide",
  "custom_path": null,
  "cleanup_after_upload": true,
  "verify_after_upload": true,
  "token": "hf_xxx"
}
```

### 8.10 存储配置数据模型

```python
@dataclass
class StorageProfile:
    id: str
    name: str
    storage_type: StorageType
    created_at: datetime
    updated_at: datetime

    endpoint: str | None = None
    region: str | None = None
    bucket: str | None = None
    access_key: str | None = None
    secret_key_encrypted: str | None = None     # AES 加密存储
    root_path: str = "/"
    path_template: str = "/models/{org}/{model_name}/{revision}/"

    upload_threads: int = 8
    multipart_threshold: int = 64 * 1024 * 1024
    multipart_chunk_size: int = 128 * 1024 * 1024
    bandwidth_limit_mbps: float = 0
    upload_retries: int = 3
    server_side_encryption: bool = False

    exists_policy: ExistsPolicy = ExistsPolicy.VERIFY_THEN_DECIDE
    upload_mode: UploadMode = UploadMode.IMMEDIATE
    cleanup_after_upload: bool = True
    verify_after_upload: bool = True

    last_test_at: datetime | None = None
    last_test_success: bool | None = None
    last_test_detail: str | None = None

    def to_config(self) -> StorageConfig:
        return StorageConfig(
            id=self.id, name=self.name, storage_type=self.storage_type,
            endpoint=self.endpoint, region=self.region, bucket=self.bucket,
            access_key=self.access_key,
            secret_key=decrypt(self.secret_key_encrypted),
            root_path=self.root_path, path_template=self.path_template,
            upload_threads=self.upload_threads,
            multipart_threshold=self.multipart_threshold,
            multipart_chunk_size=self.multipart_chunk_size,
            bandwidth_limit_mbps=self.bandwidth_limit_mbps,
            upload_retries=self.upload_retries,
            server_side_encryption=self.server_side_encryption,
            exists_policy=self.exists_policy,
            upload_mode=self.upload_mode,
            cleanup_after_upload=self.cleanup_after_upload,
            verify_after_upload=self.verify_after_upload,
        )

    def to_api_response(self) -> dict:
        return {
            "id": self.id, "name": self.name, "storage_type": self.storage_type.value,
            "endpoint": self.endpoint, "region": self.region, "bucket": self.bucket,
            "access_key": (self.access_key[:4] + "..." + self.access_key[-4:])
                          if self.access_key else None,
            "root_path": self.root_path, "path_template": self.path_template,
            "upload_threads": self.upload_threads,
            "multipart_chunk_size_mb": self.multipart_chunk_size // (1024 * 1024),
            "bandwidth_limit_mbps": self.bandwidth_limit_mbps,
            "exists_policy": self.exists_policy.value,
            "upload_mode": self.upload_mode.value,
            "cleanup_after_upload": self.cleanup_after_upload,
            "verify_after_upload": self.verify_after_upload,
            "last_test": {
                "at": self.last_test_at.isoformat() if self.last_test_at else None,
                "success": self.last_test_success,
                "detail": self.last_test_detail,
            },
         }
    ```
---

## 9. 任务优先级与排队调度

### 9.1 任务命名与目录规范

每个下载任务创建时自动生成唯一标识：

```
格式: {模型短名}_{日期}_{UUID短码}

示例:
  DeepSeek-V3_20260428_a3f8d2e1
  Kimi-K2_20260428_7b2c4fd8
  GLM-4-9b_20260428_c9e1a5b3

生成规则:
  模型短名 = repo_id.split("/")[-1]        # deepseek-ai/DeepSeek-V3 → DeepSeek-V3
  日期     = datetime.now().strftime("%Y%m%d")
  UUID短码 = uuid4().hex[:8]

目录结构(存储后端):
  /models/DeepSeek-V3_20260428_a3f8d2e1/
  ├── model-00001-of-00163.safetensors
  ├── model-00002-of-00163.safetensors
  ├── ...
  ├── config.json
  ├── tokenizer.json
  └── model.safetensors.index.json
```

### 9.2 优先级设计

```python
class Priority(int, Enum):
    CRITICAL = 0      # 最高 — 用户手动提升
    HIGH = 1          # 高
    NORMAL = 2        # 默认
    LOW = 3           # 低
    BACKGROUND = 4    # 后台 — 空闲时才执行


@dataclass
class DownloadTask:
    id: str                                     # UUID
    task_key: str                               # DeepSeek-V3_20260428_a3f8d2e1
    repo_id: str
    revision: str
    priority: Priority = Priority.NORMAL
    created_at: datetime = None
    status: str = "queued"                      # queued / active / paused / completed / failed
    is_simulation: bool = False                 # 模拟测试标记
```

### 9.3 任务队列调度器

```python
class TaskQueueScheduler:
    """
    控制同时运行的任务数量, 按优先级调度排队任务

    规则:
    1. 最大并行任务数可配置 (默认: 3)
    2. 同一时刻只有 max_concurrent 个任务处于 active 状态
    3. 排队任务按 priority ASC, created_at ASC 排序
    4. 活跃任务完成后自动拉起下一个排队任务
    5. 用户可随时调整优先级, 实时重排队列
    6. 用户可手动将排队任务立即提升为活跃 (若未超上限)
    """

    def __init__(self, max_concurrent: int = 3):
        self.max_concurrent = max_concurrent
        self.active_tasks: dict[str, DownloadTask] = {}
        self.queued_tasks: list[DownloadTask] = []

    def enqueue(self, task: DownloadTask):
        task.status = "queued"
        insort(self.queued_tasks, task, key=self._sort_key)
        self._try_promote()

    def set_priority(self, task_key: str, new_priority: Priority):
        task = self._find_task(task_key)
        if not task:
            return
        task.priority = new_priority
        if task.status == "queued":
            self.queued_tasks.sort(key=self._sort_key)
        elif task.status == "active":
            pass
        log.info(f"Task {task_key} priority changed to {new_priority.name}")

    def promote_now(self, task_key: str):
        user_force_start a queued task even if max_concurrent reached
        task = self._find_in_queue(task_key)
        if task:
            self._activate(task)

    def on_task_completed(self, task_key: str):
        if task_key in self.active_tasks:
            del self.active_tasks[task_key]
        self._try_promote()

    def _try_promote(self):
        while len(self.active_tasks) < self.max_concurrent and self.queued_tasks:
            task = self.queued_tasks.pop(0)
            self._activate(task)

    def _activate(self, task: DownloadTask):
        task.status = "active"
        self.active_tasks[task.task_key] = task
        self._dispatch_to_load_balancer(task)
        log.info(f"Task {task.task_key} activated (priority={task.priority.name})")

    def _sort_key(self, task: DownloadTask) -> tuple:
        return (task.priority, task.created_at)

    def reorder_queue(self):
        self.queued_tasks.sort(key=self._sort_key)
```

### 9.4 UI 优先级操作

```
任务队列页面 — 每个任务卡片上的操作:

排队中的任务:
  [⬆ 提升]  → priority--, 队列前移
  [⬇ 降低]  → priority++, 队列后移
  [▶ 立即开始] → 跳过排队, 强制激活 (可能超出并行上限)
  [✕ 取消]

活跃任务:
  [⏸ 暂停]  → 暂停下载, 释放执行器, 回到队列头部
  [⬆ 提升优先级] → 不影响当前, 但暂停后优先恢复
  [✕ 取消]

拖拽排序:
  用户可直接拖拽排队任务调整顺序, 自动更新 priority
```

---

## 10. 定时探查与自动下载

### 10.1 探查任务配置

```
┌─────────────────────────────────────────────────────────────────────┐
│  定时探查任务                                                [+ 新建] │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐ │
│  │ 🔄 DeepSeek-V4 发布监听                          [编辑] [删除] │ │
│  │                                                                │ │
│  │  仓库: deepseek-ai/DeepSeek-V4                                │ │
│  │  探查间隔: 每 [10] 分钟                                        │ │
│  │  探查条件:                                                     │ │
│  │    ● 仓库存在 (目前不存在, 等待首次出现)                        │ │
│  │    ● 新 commit 出现 (监听已有仓库的新版本)                      │ │
│  │    ● 特定分支出现: [___] (如 release/v4)                       │ │
│  │                                                                │ │
│  │  触发动作:                                                     │ │
│  │    ☑ 自动创建下载任务 (优先级: [CRITICAL ▼])                   │ │
│  │    ☑ 发送通知: [邮件 ▼] → admin@example.com                    │ │
│  │                                                                │ │
│  │  目标存储: [华为云OBS-北京 ▼]                                  │ │
│  │  Token: [hf_xxxxxxxxxx        ] (受限模型)                     │ │
│  │                                                                │ │
│  │  状态: 🟢 运行中  │  已探查: 142 次  │  下次探查: 3m 12s 后    │ │
│  │  上次探查: 2026-04-28 10:27:00  │  结果: 仓库不存在            │ │
│  │  创建时间: 2026-04-27 18:00:00                                 │ │
│ └────────────────────────────────────────────────────────────────┘ │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐ │
│  │ 🔄 Qwen3 发布监听                                [编辑] [删除] │ │
│  │                                                                │ │
│  │  仓库: Qwen/Qwen3-72B                                         │ │
│  │  探查间隔: 每 [30] 分钟                                        │ │
│  │  探查条件: ● 仓库存在                                          │ │
│  │  触发动作: ☑ 自动下载 (HIGH)  ☑ 通知                           │ │
│  │  状态: 🟢 运行中  │  已探查: 48 次  │  下次探查: 15m 30s 后    │ │
│  └────────────────────────────────────────────────────────────────┘ │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐ │
│  │ ✅ DeepSeek-R1 发布监听                              [已完成]  │ │
│  │  仓库: deepseek-ai/DeepSeek-R1                                │ │
│  │  已触发: 2026-04-20 03:15:00  →  任务已完成下载                 │ │
│  │  [关闭监听] [重新启用]                                         │ │
│  └────────────────────────────────────────────────────────────────┘ │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### 10.2 探查引擎实现

```python
@dataclass
class ProbeWatchConfig:
    id: str
    name: str
    repo_id: str
    interval_minutes: int = 10
    probe_condition: str = "repo_exists"    # repo_exists / new_commit / branch_exists
    branch_name: str | None = None
    auto_download: bool = True
    auto_download_priority: Priority = Priority.CRITICAL
    storage_id: str | None = None
    token: str | None = None
    notify_email: str | None = None
    status: str = "active"                 # active / triggered / disabled
    created_at: datetime = None
    last_probe_at: datetime | None = None
    last_probe_result: str | None = None
    total_probes: int = 0
    last_known_sha: str | None = None


class ProbeEngine:
    def __init__(self, hf_api: HfApi, scheduler: TaskQueueScheduler):
        self.hf_api = hf_api
        self.scheduler = scheduler
        self.watches: dict[str, ProbeWatchConfig] = {}
        self._running = False

    def add_watch(self, config: ProbeWatchConfig):
        self.watches[config.id] = config
        if not self._running:
            self._running = True
            threading.Thread(target=self._probe_loop, daemon=True).start()

    def _probe_loop(self):
        while self._running:
            now = datetime.utcnow()
            for wid, watch in list(self.watches.items()):
                if watch.status != "active":
                    continue
                if watch.last_probe_at is None or \
                   (now - watch.last_probe_at).total_seconds() >= watch.interval_minutes * 60:
                    self._execute_probe(watch)
            time.sleep(30)

    def _execute_probe(self, watch: ProbeWatchConfig):
        watch.last_probe_at = datetime.utcnow()
        watch.total_probes += 1
        triggered = False

        try:
            if watch.probe_condition == "repo_exists":
                try:
                    info = self.hf_api.model_info(watch.repo_id)
                    watch.last_probe_result = f"仓库存在, {len(info.siblings)} 文件"
                    triggered = True
                except Exception:
                    watch.last_probe_result = "仓库不存在"

            elif watch.probe_condition == "new_commit":
                try:
                    info = self.hf_api.model_info(watch.repo_id)
                    current_sha = info.sha
                    if watch.last_known_sha and current_sha != watch.last_known_sha:
                        watch.last_probe_result = f"新 commit: {current_sha[:8]}"
                        triggered = True
                    else:
                        watch.last_probe_result = f"无变化: {current_sha[:8]}"
                    watch.last_known_sha = current_sha
                except Exception as e:
                    watch.last_probe_result = f"探查失败: {e}"

            elif watch.probe_condition == "branch_exists":
                try:
                    refs = self.hf_api.list_repo_refs(watch.repo_id)
                    branches = [b.name for b in refs.branches]
                    if watch.branch_name in branches:
                        watch.last_probe_result = f"分支 {watch.branch_name} 已出现"
                        triggered = True
                    else:
                        watch.last_probe_result = f"分支不存在, 现有: {branches[:5]}"
                except Exception:
                    watch.last_probe_result = "仓库不存在"

        except Exception as e:
            watch.last_probe_result = f"探查异常: {e}"

        if triggered:
            self._on_triggered(watch)

    def _on_triggered(self, watch: ProbeWatchConfig):
        watch.status = "triggered"
        log.info(f"Probe triggered: {watch.repo_id} — {watch.last_probe_result}")

        if watch.auto_download:
            task = self.scheduler.create_task(
                repo_id=watch.repo_id,
                revision="main",
                storage_id=watch.storage_id,
                token=watch.token,
                priority=watch.auto_download_priority,
            )
            log.info(f"Auto-download task created: {task.task_key}")

        if watch.notify_email:
            self._send_notification(watch)

    def _send_notification(self, watch: ProbeWatchConfig):
        pass
```

### 10.3 探查 API

```
GET    /api/probes                      列出所有探查任务
POST   /api/probes                      创建探查任务
PUT    /api/probes/{probe_id}           更新探查任务
DELETE /api/probes/{probe_id}           删除探查任务
POST   /api/probes/{probe_id}/test      手动触发一次探查
POST   /api/probes/{probe_id}/pause     暂停探查
POST   /api/probes/{probe_id}/resume    恢复探查
GET    /api/probes/{probe_id}/history   探查历史记录
```

---

## 11. 模型完整文件清单策略

### 11.1 必须下载的文件分类

```
一个完整的 HuggingFace 模型仓库包含以下类别的文件:

类别 A — 核心权重文件 (必须):
  model-NNNNN-of-MMMMM.safetensors          # 分片权重
  model.safetensors                          # 单文件权重 (小模型)
  model.safetensors.index.json               # 分片索引 (关键!)

类别 B — 模型配置 (必须):
  config.json                                # 模型架构配置 (关键!)
  generation_config.json                     # 生成参数

类别 C — Tokenizer (必须):
  tokenizer.json                             # Fast tokenizer 数据
  tokenizer_config.json                      # Tokenizer 配置
  tokenizer.model                            # SentencePiece 模型
  tiktoken.model                             # Tiktoken 模型 (部分模型)
  tokenization_*.py                          # 自定义 Tokenizer 代码
  chat_template.jinja                        # 聊天模板

类别 D — 自定义代码 (条件必须):
  modeling_*.py                               # 自定义模型代码
  configuration_*.py                         # 自定义配置代码
  当 config.json 中存在 "auto_map" 字段时, 这些文件必须下载

类别 E — 元数据 (推荐):
  README.md                                  # 模型卡片
  .gitattributes                             # LFS 追踪规则

类别 F — 可选:
  LICENSE / LICENSE-*                        # 许可证
  *.png / *.jpg                              # 图片
  preprocessor_config.json                   # 预处理器配置 (多模态)
  special_tokens_map.json                    # 特殊 token 映射
```

### 11.2 智能文件分类引擎

```python
class ModelFileClassifier:
    """
    根据 HuggingFace 仓库的文件列表, 自动分类并生成下载清单
    """

    WEIGHT_PATTERNS = [
        re.compile(r"model-\d+-of-\d+\.safetensors"),
        re.compile(r"model\.safetensors"),
        re.compile(r"pytorch_model-\d+-of-\d+\.bin"),
        re.compile(r"pytorch_model\.bin"),
        re.compile(r"model-\d+-of-\d+\.safetensors\.index\.json"),
    ]

    INDEX_PATTERNS = [
        re.compile(r"model\.safetensors\.index\.json"),
        re.compile(r"pytorch_model\.bin\.index\.json"),
    ]

    CONFIG_PATTERNS = [
        re.compile(r"config\.json"),
        re.compile(r"generation_config\.json"),
        re.compile(r"preprocessor_config\.json"),
    ]

    TOKENIZER_PATTERNS = [
        re.compile(r"tokenizer\.json"),
        re.compile(r"tokenizer_config\.json"),
        re.compile(r"tokenizer\.model"),
        re.compile(r"tiktoken\.model"),
        re.compile(r"special_tokens_map\.json"),
        re.compile(r"tokenization_.*\.py"),
        re.compile(r"chat_template\.jinja"),
    ]

    CUSTOM_CODE_PATTERN = re.compile(r"(modeling_|configuration_|tokenization_)_.+\.py")

    def classify(self, repo_id: str, revision: str = "main",
                 token: str = None) -> FileClassification:
        api = HfApi(token=token)
        info = api.model_info(repo_id, revision=revision, files_metadata=True)

        files = []
        auto_map_files = set()
        custom_code_names = set()

        for sibling in info.siblings:
            f = ClassifiedFile(
                name=sibling.rfilename,
                size=sibling.size or 0,
                sha256=sibling.lfs.sha256 if sibling.lfs else None,
                is_lfs=sibling.lfs is not None,
                category="optional",
                required=True,
            )

            if self._match(f.name, self.WEIGHT_PATTERNS):
                f.category = "weight"
                f.required = True
            elif self._match(f.name, self.INDEX_PATTERNS):
                f.category = "index"
                f.required = True
            elif self._match(f.name, self.CONFIG_PATTERNS):
                f.category = "config"
                f.required = True
            elif f.name == "config.json":
                f.category = "config"
                f.required = True
            elif self._match(f.name, self.TOKENIZER_PATTERNS):
                f.category = "tokenizer"
                f.required = True
            elif self.CUSTOM_CODE_PATTERN.match(f.name):
                f.category = "custom_code"
                custom_code_names.add(f.name)
            elif f.name == "README.md":
                f.category = "metadata"
                f.required = False
            elif f.name == ".gitattributes":
                f.category = "metadata"
                f.required = True
            elif f.name.startswith("LICENSE"):
                f.category = "license"
                f.required = False
            else:
                f.category = "optional"
                f.required = False

            files.append(f)

        # 解析 config.json 的 auto_map, 标记依赖的自定义代码为必须
        config_content = self._fetch_config_content(api, repo_id, revision, files)
        if config_content and "auto_map" in config_content:
            for code_file in config_content["auto_map"].values():
                if isinstance(code_file, str):
                    auto_map_files.add(code_file)
                elif isinstance(code_file, list):
                    auto_map_files.update(code_file)

        for f in files:
            if f.name in auto_map_files:
                f.category = "custom_code_required"
                f.required = True

        total_size = sum(f.size for f in files)
        required_size = sum(f.size for f in files if f.required)

        return FileClassification(
            repo_id=repo_id,
            revision=revision,
            files=files,
            total_files=len(files),
            total_size=total_size,
            required_files=sum(1 for f in files if f.required),
            required_size=required_size,
            optional_files=sum(1 for f in files if not f.required),
            optional_size=total_size - required_size,
            auto_map_files=list(auto_map_files),
        )

    def _match(self, name: str, patterns: list) -> bool:
        return any(p.match(name) for p in patterns)

    def _fetch_config_content(self, api, repo_id, revision, files):
        for f in files:
            if f.name == "config.json":
                try:
                    path = hf_hub_download(repo_id, "config.json", revision=revision)
                    with open(path) as fp:
                        return json.load(fp)
                except Exception:
                    return None
        return None


@dataclass
class ClassifiedFile:
    name: str
    size: int
    sha256: str | None
    is_lfs: bool
    category: str       # weight / index / config / tokenizer / custom_code / custom_code_required / metadata / license / optional
    required: bool


@dataclass
class FileClassification:
    repo_id: str
    revision: str
    files: list[ClassifiedFile]
    total_files: int
    total_size: int
    required_files: int
    required_size: int
    optional_files: int
    optional_size: int
    auto_map_files: list[str]
```

### 11.3 UI 文件选择界面

```
┌───────────────────────────────────────────────────────────────────┐
│  deepseek-ai/DeepSeek-V3 — 文件选择                         [✕] │
├───────────────────────────────────────────────────────────────────┤
│                                                                    │
│  ☑ 全部选择 (170 文件, 689 GB)                                    │
│                                                                    │
│  ── 权重文件 (163 文件, 684.7 GB) — 必须 ──────────────────────  │
│  ☑ model-00001-of-00163.safetensors        4.30 GB   LFS         │
│  ☑ model-00002-of-00163.safetensors        4.30 GB   LFS         │
│  ☑ ...                                         (161 more)        │
│  ☑ model.safetensors.index.json            8.90 MB              │
│                                                                    │
│  ── 模型配置 (2 文件, 1.4 KB) — 必须 ────────────────────────── │
│  ☑ config.json                             1.20 KB              │
│  ☑ generation_config.json                  0.20 KB              │
│                                                                    │
│  ── Tokenizer (3 文件, 8.5 MB) — 必须 ───────────────────────── │
│  ☑ tokenizer.json                          8.50 MB              │
│  ☑ tokenizer_config.json                   6.00 KB              │
│  ☑ tokenization_deepseek.py                9.20 KB              │
│                                                                    │
│  ── 自定义代码 (2 文件, 45 KB) — 必须 (auto_map 依赖) ──────── │
│  ☑ configuration_deepseek.py               12 KB  ← config引用  │
│  ☑ modeling_deepseek.py                     33 KB  ← model引用   │
│                                                                    │
│  ── 元数据 (2 文件, 15 KB) — 推荐 ──────────────────────────── │
│  ☑ .gitattributes                          1.50 KB              │
│  ☐ README.md                               13.5 KB              │
│                                                                    │
│  ── 许可证 (1 文件, 1.1 KB) — 可选 ─────────────────────────── │
│  ☐ LICENSE                                 1.10 KB              │
│                                                                    │
│  ── 汇总 ────────────────────────────────────────────────────── │
│  必须文件: 170 个 (689 GB)                                        │
│  推荐文件: 171 个 (689 GB)                                        │
│  全部文件: 172 个 (689 GB)                                        │
│                                                                    │
│  ⚠ 注意: 去掉 config.json 或自定义代码文件将导致模型无法加载      │
│                                                                    │
│                                           [取消]  [确认选择]      │
└───────────────────────────────────────────────────────────────────┘
```

---

## 12. 模拟测试模式

### 12.1 设计目标

模拟测试模式用于快速验证整个下载系统的端到端流程，无需真正下载完整的大文件。

**核心思路：**
- 每个文件仅下载前 1KB 数据
- 快速走完 下载 → 校验 → 上传 → 远端校验 全流程
- 在执行器和存储后端都创建独立的 simu 目录
- 验证控制器调度、执行器注册、心跳、负载均衡等全部逻辑

### 12.2 目录命名规范

```
正常下载任务目录:
  {模型短名}_{日期}_{UUID8}
  例: DeepSeek-V3_20260428_a3f8d2e1

模拟测试任务目录:
  simu_{模型短名}_{日期}_{UUID8}
  例: simu_DeepSeek-V3_20260428_b4d7e9f2

执行器本地临时目录:
  正常: /tmp/downloads/{task_key}/
  模拟: /tmp/downloads/simu_{task_key}/

存储后端目标路径:
  正常: obs://bucket/models/{task_key}/
  模拟: obs://bucket/models/simu_{task_key}/
```

### 12.3 模拟模式实现

```python
class SimulationConfig:
    download_bytes_limit: int = 1024          # 每文件仅下载 1KB
    skip_sha256_verify: bool = True           # 跳过 SHA256 (1KB 不匹配完整文件)
    skip_upload: bool = False                 # 仍然执行上传 (测试上传流程)
    fake_speed_multiplier: int = 1            # 速度倍率 (1=正常上报, 可调大模拟高速)
    auto_cleanup_hours: int = 24              # 24小时后自动清理模拟数据


class SimulationDownloader:
    """模拟下载器: 每个文件只下载前 N 字节"""

    def __init__(self, config: SimulationConfig):
        self.config = config

    def download_file(self, url: str, target_path: str,
                      expected_size: int, headers: dict = None,
                      progress_callback=None) -> DownloadResult:
        os.makedirs(os.path.dirname(target_path), exist_ok=True)

        limit = min(self.config.download_bytes_limit, expected_size)
        headers = {**(headers or {}), "Range": f"bytes=0-{limit - 1}"}

        response = requests.get(url, headers=headers, stream=True, timeout=30)
        response.raise_for_status()

        downloaded = 0
        with open(target_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=1024):
                to_write = min(len(chunk), limit - downloaded)
                f.write(chunk[:to_write])
                downloaded += to_write
                if progress_callback:
                    progress_callback(downloaded, expected_size)
                if downloaded >= limit:
                    break

        return DownloadResult(
            status="simulated",
            path=target_path,
            actual_bytes=downloaded,
            simulated=True,
        )

    def compute_simulated_sha256(self, file_path: str, expected_size: int,
                                  expected_sha256: str) -> str:
        if self.config.skip_sha256_verify:
            return "SIMULATION_SKIPPED"
        return super().compute_sha256(file_path)


class SimulationStorageBackend(StorageBackend):
    """模拟存储后端: 包装真实后端, 写入 simu_ 前缀路径"""

    def __init__(self, real_backend: StorageBackend, simu_prefix: str = "simu_"):
        self.real_backend = real_backend
        self.simu_prefix = simu_prefix

    def upload_file(self, local_path: str, remote_path: str,
                    expected_sha256: str = None) -> UploadResult:
        simu_path = f"{self.simu_prefix}{remote_path}"
        result = self.real_backend.upload_file(local_path, simu_path, expected_sha256)
        result.remote_path = simu_path
        result.simulated = True
        return result

    def test_connection(self) -> ConnectionTestResult:
        return self.real_backend.test_connection()

    def exists(self, remote_path: str) -> bool:
        return self.real_backend.exists(f"{self.simu_prefix}{remote_path}")

    def delete(self, remote_path: str) -> bool:
        return self.real_backend.delete(f"{self.simu_prefix}{remote_path}")
```

### 12.4 模拟模式 Controller 集成

```python
class Controller:
    def create_simulation_task(self, repo_id: str, revision: str = "main",
                                storage_id: str = None,
                                token: str = None) -> DownloadTask:
        """
        创建模拟测试任务:
        1. 获取仓库完整文件列表 (真实 API 调用)
        2. 每个文件标记为 simulated=True
        3. 生成 simu_ 前缀的 task_key
        4. 注入 SimulationDownloader 和 SimulationStorageBackend
        """
        simu_config = SimulationConfig()
        classification = ModelFileClassifier().classify(repo_id, revision, token)

        task_key = f"simu_{classification.repo_id.split('/')[-1]}_" \
                   f"{datetime.utcnow().strftime('%Y%m%d')}_{uuid4().hex[:8]}"

        task = DownloadTask(
            id=str(uuid4()),
            task_key=task_key,
            repo_id=repo_id,
            revision=revision,
            status="queued",
            priority=Priority.CRITICAL,
            is_simulation=True,
            storage_id=storage_id,
            token=token,
            simulation_config=simu_config,
        )

        for f in classification.files:
            subtask = FileSubTask(
                id=str(uuid4()),
                task_id=task.id,
                filename=f.name,
                file_size=f.size,
                expected_sha256=f.sha256,
                simulated=True,
            )
            task.subtasks.append(subtask)

        task.total_files = len(task.subtasks)
        task.total_size = sum(s.file_size for s in task.subtasks)

        self.db.save(task)
        self.scheduler.enqueue(task)
        return task

    def cleanup_simulation(self, task_key: str):
        """清理模拟测试数据: 删除执行器本地文件 + 存储后端 simu_ 目录"""
        task = self.db.get_task_by_key(task_key)
        if not task or not task.is_simulation:
            return

        for subtask in task.subtasks:
            if subtask.executor_id:
                self._request_executor_cleanup(subtask.executor_id, subtask)

        storage = self.storage_manager.get_backend(task.storage_id)
        if storage:
            files = storage.list_files(f"simu_{task_key}/")
            for f in files:
                storage.delete(f.key)
            log.info(f"Simulation cleaned up: {task_key}")
```

### 12.5 模拟模式 UI

```
┌───────────────────────────────────────────────────────────────────┐
│  模拟测试                                                  [✕]   │
├───────────────────────────────────────────────────────────────────┤
│                                                                    │
│  选择模型: [deepseek-ai/DeepSeek-V3 ▼] 或输入自定义仓库ID        │
│  仓库ID:  [_____________________________]                         │
│                                                                    │
│  目标存储: [华为云OBS-北京 ▼]                                      │
│  Token:    [hf_xxxxxxxxxx        ]                                │
│                                                                    │
│  ── 模拟参数 ──────────────────────────────────────────────────  │
│                                                                    │
│  每文件下载字节数: [1024 ▼] B / KB / MB                           │
│  跳过 SHA256 校验: [☑] (模拟文件不匹配完整哈希)                   │
│  执行上传到存储:   [☑] (测试完整上传流程)                          │
│  模拟完成后自动清理: [☐]  清理延迟: [24] 小时                     │
│                                                                    │
│  ── 预估 ────────────────────────────────────────────────────── │
│                                                                    │
│  文件数: 170 (含权重163+配置7)                                     │
│  模拟数据量: ~170 KB (每文件1KB)                                   │
│  预计耗时: < 2 分钟                                                │
│  存储路径: simu_DeepSeek-V3_20260428_xxxxxxxx/                     │
│                                                                    │
│  ── 测试范围 ────────────────────────────────────────────────── │
│                                                                    │
│  此模拟将测试以下完整流程:                                         │
│  ✅ HuggingFace API 连接与认证                                     │
│  ✅ 模型文件列表获取                                               │
│  ✅ 任务创建与排队                                                 │
│  ✅ 执行器注册与心跳                                               │
│  ✅ 任务调度与分配 (负载均衡)                                      │
│  ✅ 多线程分块下载 (每文件1KB)                                     │
│  ✅ 断点续传逻辑                                                   │
│  ✅ 上传到存储后端                                                 │
│  ✅ 三阶段校验流程 (下载/控制器/远端)                              │
│  ✅ 进度上报与 WebSocket 推送                                      │
│  ✅ 文件完整性矩阵展示                                             │
│  ✅ 任务状态机完整流转                                             │
│                                                                    │
│                           [取消]  [开始模拟测试]                   │
│                                                                    │
└───────────────────────────────────────────────────────────────────┘

模拟测试运行时 — 与真实下载共用同一套 UI, 区别在于:

┌───────────────────────────────────────────────────────────────────┐
│ Tab: 🧪 simu_DeepSeek-V3                              [SIMULATION]│
├───────────────────────────────────────────────────────────────────┤
│                                                                    │
│  ⚠ 模拟测试模式 — 每文件仅下载 1KB, 校验已跳过                    │
│                                                                    │
│  ┌─ 总览 ───────────────────────────────────────────────────────┐ │
│  │ 总进度: 45.3% (77/170 文件)  │  耗时: 38s  │  预计: ~1m 20s  │ │
│  │ 下载: 0.5 MB/170 KB (模拟)   │  上传: 0.3 MB (模拟)         │ │
│  └───────────────────────────────────────────────────────────────┘ │
│                                                                    │
│  ┌─ 文件矩阵 ───────────────────────────────────────────────────┐│
│  │ ✅✅✅✅✅✅✅✅✅✅  (已下载+上传 1KB)                          ││
│  │ ✅✅✅✅✅✅✅✅✅✅                                            ││
│  │ ✅✅✅✅✅⬛⬛⬛⬛⬛                                            ││
│  │ ⬛⬛⬛⬛⬛⬛⬛⬛⬛⬛                                            ││
│  │ ⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜                                            ││
│  │                                                               ││
│  │ ✅ 77 完成(模拟)  ⬛ 20 下载中  ⬜ 73 等待                    ││
│  └───────────────────────────────────────────────────────────────┘│
│                                                                    │
│  ┌─ 文件详情 (校验列显示模拟状态) ───────────────────────────────┐│
│  │ 文件                     │大小    │进度 │下载校验│远端校验     ││
│  │ model-00001 (1KB模拟)    │4.3 GB  │100% │⚠ 模拟 │✅ 模拟通过 ││
│  │ model-00002 (1KB模拟)    │4.3 GB  │100% │⚠ 模拟 │✅ 模拟通过 ││
│  │ config.json (1KB模拟)    │1.2 KB  │100% │⚠ 模拟 │✅ 模拟通过 ││
│  │ model-00078 (下载中)     │4.3 GB  │ 45% │  -    │  -         ││
│  │ ...                                                          ││
│  │                                                               ││
│  │ ⚠ = 模拟模式, 仅下载1KB, SHA256校验已跳过                    ││
│  └───────────────────────────────────────────────────────────────┘│
│                                                                    │
│  [清理模拟数据]  [基于此配置创建真实下载任务]                       │
│                                                                    │
└───────────────────────────────────────────────────────────────────┘
```

### 12.6 模拟测试 API

```
POST /api/tasks/simulate
     Body: {
       "repo_id": "deepseek-ai/DeepSeek-V3",
       "revision": "main",
       "storage_id": "storage-huawei-obs-01",
       "token": "hf_xxx",
       "download_bytes_limit": 1024,
       "skip_sha256_verify": true,
       "perform_upload": true,
       "auto_cleanup_hours": 24
     }
     Response: {
       "task_id": "...",
       "task_key": "simu_DeepSeek-V3_20260428_b4d7e9f2",
       "total_files": 170,
       "estimated_data": "170 KB",
       "estimated_time": "< 2 minutes",
       "storage_path": "simu_DeepSeek-V3_20260428_b4d7e9f2/"
     }

POST /api/tasks/{task_key}/cleanup-simulation
     Response: {
       "deleted_files": 170,
       "deleted_bytes": 174080,
       "storage_cleaned": true,
       "executor_temp_cleaned": true
     }

POST /api/tasks/{task_key}/promote-to-real
     Body: {
       "priority": "NORMAL",
       "storage_id": "storage-huawei-obs-01"    // 可切换存储
     }
     Response: {
       "real_task_id": "...",
       "real_task_key": "DeepSeek-V3_20260428_c5e8f1a3",
       "status": "queued"
     }
```

### 12.7 模拟测试全链路验证清单

```
模拟测试自动验证以下检查项:

[ ] HF API 连接正常, 可搜索模型
[ ] 可获取模型文件列表及 LFS SHA256
[ ] 任务创建成功, 进入排队
[ ] 执行器注册成功, 心跳正常
[ ] 控制器正确调度任务到执行器
[ ] 负载均衡: 文件均匀分配到各执行器
[ ] 执行器启动多线程下载 (1KB)
[ ] 断点续传: Range 请求正确
[ ] 进度实时上报到控制器
[ ] WebSocket 推送到 UI
[ ] 文件下载完成后本地校验 (模拟跳过)
[ ] 上传到存储后端成功
[ ] 远端文件存在且可读
[ ] 校验矩阵 UI 正确显示三列校验状态
[ ] 任务状态机完整流转 queued → active → completed
[ ] 模拟数据目录命名正确 (simu_ 前缀)
[ ] 清理功能正常删除所有临时数据
```


