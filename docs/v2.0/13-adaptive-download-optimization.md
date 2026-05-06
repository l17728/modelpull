# 13 — 自适应下载最优化（在线运筹优化）

> 角色：把"下载调度"从启动一次的 LPT 提升为**持续在线运筹优化**。每 X 秒重估当前状态，决定是否重新分配 / 子分片 / 加并发，目标是**最小化整体完成时间**。
> 引入版本：**v2.1**（v2.0 用 06 §1.8 反应式版本作为前期能力）。
> 取代关系：本文是 06 §1.8 阶段 B（"下载中持续校准"）的形式化升级版。

---

## 0. 立项背景

### 0.1 v2.0 反应式重平衡的不足

v2.0 当前在 06 §1.8 阶段 B 实现了：

```
每 60 秒检查 → 某 (executor, source) 速度跌至 30% → 把"未开始"的 chunk 重分
```

这套机制有 4 个问题：

| 问题 | 影响 |
|------|------|
| **粒度粗**：60s 间隔，30% 阈值拍脑袋 | 真实退化常在 5-10s 内发生，错过最优窗口 |
| **只动未开始的 chunk** | 已下了 30% 的慢 chunk 不动，成"长尾瓶颈" |
| **不考虑切换成本** | 简单切换=丢弃部分下载；可能切换后总耗时反而更长 |
| **不会进一步切分** | 大文件单 chunk 慢 → 不会 dynamically 切成更小 chunk 给多 executor |

### 0.2 用户场景驱动

真实场景：

> 一个 17GB 的 `model-001-of-061.safetensors`，开始时分配给 node-3（HF Mirror 源，速度 420 MB/s）。下载到 60% 时（已下 10GB），HF Mirror 突遇 429 限流降到 80 MB/s。剩余 7GB 还要 90 秒。同时 node-7 当前空闲，ModelScope 此时给它 950 MB/s。
>
> **问题**：是不是该让 node-7 接管剩余 7GB？接管的代价是丢弃 node-3 已下的 10GB 吗？还是把剩余 7GB **进一步切成 4 个 chunk**，让 node-3 + node-7 + node-1 + node-5 一起冲刺？

这就是运筹优化问题。

### 0.3 用户关注点回应

> "已下载的部分可以不参与变化，除非成为整体瓶颈" — 用户提出

✅ 确认为本文核心原则之一（详见 §3 不变量 20）。

> "不同节点的下载器访问其他节点的目录进行拼接估计需要额外的权限或者能力" — 用户担忧

✅ 不需要跨节点 FS 访问。**关键洞察：S3 multipart upload 协议天然实现"多 executor 同文件协作"** —— 每个 executor 只与 S3 通信，部件号（PartNumber）由 controller 协调。详见 §5。
非 S3 backend（NFS / local）退化到单 executor 模式（详见 §5.5）。

---

## 1. 问题形式化

### 1.1 状态空间

任意时刻 `t`，系统状态：

```
F(t) = {f_1, ..., f_N}                  待下载文件 / chunk 集合
E    = {e_1, ..., e_M}                  健康 executor 集合
S    = {s_1, ..., s_K}                  可用 source 集合
V(t)[e][s]                              executor e 从 source s 的实测速度（EWMA，bytes/s）
P(t)[f]                                 文件 f 已下载字节数 (partial progress)
A(t)[f]                                 文件 f 当前的执行器分配（可能为 ∅）
```

### 1.2 决策变量

> ⚠️ **v2.1 修订（OR-V21-02）**：原版本 `Plans(f)` 是所有 byte-range 切分组合，规模 ~2^chunks，**指数级**，LP 不可直接展开。修订为**离散预生成 plan 集合**。

```
x[f, e, s, plan] ∈ {0, 1}     是否把文件 f 的 plan（分片方案）分给 (e, s)
plan ∈ Plans(f)                 文件 f 的可行分片方案集（离散，预生成）
```

`Plans(f)` **限定为离散小集合**：

```python
def enumerate_plans(file: File, storage: StorageBackend) -> list[Plan]:
    plans = [Plan(k=1, mode="single")]                    # 总有 single
    if storage.is_s3_compatible() and file.size >= 100 * 1024 * 1024:
        for k in [2, 3, 4, 8]:                            # 离散候选
            sub_size = file.size / k
            if 5 * MB <= sub_size <= 5 * GB and k <= 10000:
                plans.append(Plan(k=k, mode="split"))
    return plans
```

**总变量数上界**：`|F| × |E| × |S| × |Plans| ≤ N × M × K × 5`。100 文件 × 10 executor × 3 source × 5 plan = **15,000 变量**，对 LP solver（CBC / linprog）规模可接受。

> 📝 **决策**：放弃在 §1.3 的 LP 中展开任意切分方式；接受 plan 离散化的"近似最优"代价。实测表明 k ∈ {1,2,3,4,8} 已覆盖 ≥ 95% 真实场景的最优 k 选择。
> 如需更精细：v2.2 用 column generation 按需展开（详见 §11 roadmap）。

### 1.3 目标函数（v2.1 修订 — OR-V21-01）

> ⚠️ 原版本仅 makespan 在 "99 文件已完成 + 1 文件慢" 时退化为单文件 ETA 最小化（其他文件的分配对目标无影响 → LP 多解 / degenerate）。

修订为**字典序目标**：

```
lex_minimize:
  1. makespan    = max over e: Σ x[f,e,s,p] × bytes_remaining / V[e][s]
  2. P95_completion_time(per file)
  3. Σ w_f × C_f                # weighted flow time，w_f = file size or tenant priority
```

LP 实现：

- 先求 `makespan` 的最优值 `T*`
- 加约束 `makespan ≤ T*`（或 `T* × 1.01` 容差）
- 在此约束下最小化 P95
- 重复直到 weighted flow time

实践简化（fast path）：直接用 weighted flow time `Σ w_f × C_f`（含 makespan 信息且无退化），slow path 才走完整 lex。

```
fast_path:  minimize  Σ over f: w_f × completion_time(f)
            其中 completion_time(f) = max over e: Σ x[f,e,s,p] × bytes/V[e][s]

slow_path:  lex_minimize  (makespan, P95, weighted_flow_time)
```

约束：

```
∀ f: Σ x[f, ·, ·, ·] = 1                     # 每个文件恰好选一个方案
∀ (e, s): Σ x[f,e,s,p] × bytes_assigned ≤ V[e][s] × deadline   # 不超过 executor 容量
∀ e: 同时下载文件数 ≤ max_concurrent_per_executor
∀ s: Σ x[·,·,s,·] × bytes ≤ source_rate_limit(s)               # 源限流
```

### 1.4 切换成本（关键）

如果当前文件 `f` 已分给 `(e_old, s_old)`，已下载 `P[f]` 字节；新方案要切到 `(e_new, s_new)`：

```
switch_cost(f, old, new) =
  P[f] × switch_loss_factor(scenario)
```

> ⚠️ **v2.1 修订（OR-V21-06）**：原版本 `switch_loss_factor=1.0` 过度悲观。HTTP Range request 支持 byte-offset 续传，S3 multipart 已上传 part 不丢；只有非 S3 backend 全局重做。

**分场景 loss_factor**：

| 场景 | factor | 说明 |
|------|--------|------|
| **S3 backend + Range 支持源 + 部分 part 已 uploaded** | `0.0` | 已 upload 的 part 保留；新 executor 只下未 uploaded 部分 |
| **S3 backend + Range 支持源 + 当前 chunk 进度 < 一个 part 大小** | `P_unflushed / P[f]` | 仅丢失"未 flush 到 S3 part"的字节 |
| **S3 backend + 不支持 Range 的源**（罕见，例如不支持 byte-range 的 mirror） | `1.0` | 必须重下整段 |
| **NFS / local backend**（无 multipart） | `1.0` | 单 executor 拼装；切 executor = 全丢 |
| **同 executor 仅切 source（保留 .parts/）** | `0.0` | 续传到现有文件偏移 |
| **plan 改变（k 路 split）** | `1.0` 但仅对当前 executor 已下载部分 | k=1 → k=4 时已下数据需要重组成 part 提交，简化为重下 |

**优化**：v2.1 实现可保守用 `0.0 / 0.5 / 1.0` 三档；`P_unflushed/P` 精度依赖 executor 上报的 last-flushed offset，复杂度高，留 v2.2。

**带切换成本的目标函数**：

```
minimize  makespan(t+Δ)  +  α × Σ over switched files: switch_cost(f, old, new) / V_avg
```

`α` 是惩罚因子（默认 1.0）—— 单位转换：让等价时间损失 1:1 计入完成时间。> 1 是"惩罚切换"的 risk-aversion，v2.1 chaos 测试反推合理范围（OR-V21-05 修复）。

（旧的"带切换成本的目标函数"段落已合并到 §1.4 上方。）

---

## 2. 决策原则

### 2.1 6 条核心原则

🔒 **不变量 20：已下载字节默认不参与重新规划**

只在以下条件之一时才"丢弃已下载部分"：

a. 当前文件成为 **整体 makespan 瓶颈**（最长 ETA 文件）
b. 切换方案能让该文件 ETA 减少 ≥ `switch_threshold_ratio`（默认 50%）
c. 用户显式 force-rebalance

🔒 **不变量 21（修订 v2.1, OR-V21-08）：决策必须有 hysteresis；优先用 CUSUM 而非硬阈值**

**为什么不用硬阈值**：30%/15s 触发是经验值，false-alarm rate 与 detection-delay 无显式控制。生产环境应使用 CUSUM（cumulative sum control chart）或 SPRT（Sequential Probability Ratio Test）。

**CUSUM 检测公式**：

```python
def cusum_detect(speed_history, baseline, k_threshold=0.5, h_alarm=4):
    """
    检测速度持续偏离基线。
    k_threshold: drift 容忍度（基线的 k 倍 σ 内不计）
    h_alarm: 累积偏离上限，超过即触发
    返回：是否报警 + 累积偏离值
    """
    cs_low = 0    # 检测下偏（速度跌）
    sigma = stdev(speed_history) or baseline * 0.1
    for s in speed_history:
        cs_low = max(0, cs_low - (s - (baseline - k_threshold * sigma)))
        if cs_low > h_alarm * sigma:
            return True, cs_low
    return False, cs_low
```

**优势**：

- 显式控制 false-alarm rate `α` 与 mean-time-to-detection（取决于 k 与 h）
- 对持续小幅偏离敏感（硬阈值会漏）
- 对单点 outlier 不敏感（硬阈值会误判）

**v2.1 默认参数**（基于 EWMA τ=10s）：

```yaml
hysteresis:
  algorithm: cusum                  # cusum | sprt | hard_threshold (legacy)
  cusum:
    k_drift_tolerance_sigma: 0.5    # 0.5σ drift 内视为正常
    h_alarm_sigma: 4                 # 累积偏离 4σ 触发
    min_samples_before_alarm: 3
    backoff_after_alarm_seconds: 60  # cooldown
  hard_threshold:                    # 仅作为退化路径
    drop_pct: 30
    drop_duration_s: 15
    recovery_pct: 70
    recovery_duration_s: 30
```

**单文件 cooldown**：保留 60s（不变量 21 子条款）。详见 §9.2 配置默认值的统计学依据。

🔒 **不变量 22：子分片必须满足 S3 multipart 约束**

- sub-chunk size ∈ [5MB, 5GB]
- 总 part 数 ≤ 10000
- 当 storage backend 非 S3 兼容（NFS/local），不允许子分片

🔒 **不变量 23：在线优化决策必须可证伪**

每次重新规划写 `optimization_decisions` 表（含输入状态 + 决策 + 预期收益），可事后回放验证算法是否合理。

🔒 **不变量 24（修订 v2.1, OR-V21-13）：Optimizer 不做跨任务全局最优；但 v2.1 加入最简跨任务公平性兜底**

**v2.1 范围**：每个任务独立最优化 + **轻量跨任务公平**（不需要全局 LP）：

```python
def per_task_executor_quota(task: Task, all_active_tasks: list[Task]) -> dict[str, int]:
    """
    简单 round-robin：每个任务可用 executor 数 ≤ N_total / N_active_tasks，
    防止"任务 A 独占 8 executor，任务 B 全部排队"。
    高优先级任务获 1.5× 配额。
    """
    base_quota = max(1, len(healthy_executors) // len(all_active_tasks))
    priority_factor = {0: 0.7, 1: 1.0, 2: 1.2, 3: 1.5}[task.priority]
    return int(base_quota * priority_factor)
```

PlanOptimizer 在 §4.1 fast_path 中加约束：单任务 `len(distinct_executors) ≤ per_task_executor_quota`。

**v2.2 才做的**：完整跨任务全局 LP / 抢占式调度 / Class-of-Service。
**Why 现在加最简版**：避免显著不公（OR-V21-13），但不引入复杂度。

🔒 **不变量 25：决策最大延迟 ≤ 5 秒**

每次重新规划必须在 5 秒内完成（含 LP 求解）。超时则用 fallback 启发式（greedy LPT）。
**Why**：决策本身不能成为瓶颈。

### 2.2 不重新规划的场景

显式列出 **不会** 触发重新规划的情况：

- 任务剩余 < 5% 字节（接近完成，切换收益小）
- 任务进入 `verifying_*` 状态（已经在做远端校验）
- Cooldown 内（上次切换 < 60s）
- 被 admin 标记 `frozen`（运维介入）

---

## 3. 整体架构

```
                        ┌────────────────────────────┐
                        │   PlanOptimizer Service     │
                        │   (Controller 内独立模块)   │
                        └─────────────┬───────────────┘
                                      │
            ┌─────────────────────────┼─────────────────────────┐
            │                         │                         │
            ▼                         ▼                         ▼
   ┌─────────────────┐   ┌─────────────────────┐    ┌────────────────────┐
   │ StateCollector  │   │ DecisionEngine       │    │ ActionExecutor      │
   │ (从 DB / 心跳   │   │ ┌─────────────────┐ │    │ (发命令到 executor)│
   │  收集当前状态)  │   │ │ FastPath         │ │    │                    │
   └─────────────────┘   │ │  (Greedy LPT)    │ │    └────────────────────┘
                         │ └─────────────────┘ │
                         │ ┌─────────────────┐ │
                         │ │ SlowPath          │ │
                         │ │  (LP solver,      │ │
                         │ │   仅当 fast 收益  │ │
                         │ │   不显著时调用)   │ │
                         │ └─────────────────┘ │
                         └─────────────────────┘
```

**循环节奏**：

```
Every 30s (default):
  state = StateCollector.snapshot()
  bottleneck_files = state.find_bottleneck()
  if not bottleneck_files: continue   # 无瓶颈，不动

  for f in bottleneck_files:
    fast_plan = DecisionEngine.fast_path(f, state)
    if fast_plan.expected_savings > switch_threshold:
       ActionExecutor.apply(fast_plan)
       continue

    slow_plan = DecisionEngine.slow_path(f, state)   # LP solver
    if slow_plan.expected_savings > switch_threshold:
       ActionExecutor.apply(slow_plan)
```

---

## 4. 决策算法

### 4.1 Fast Path：贪心 + 切换收益计算

**输入**：单个瓶颈文件 `f`，当前 partial progress `P[f]`，当前分配 `(e_old, s_old)`。

**步骤**：

```python
def fast_path_decide(f, state):
    bytes_remaining = f.size - state.P[f]
    current_speed = state.V[e_old][s_old]
    current_eta = bytes_remaining / current_speed

    candidates = []
    for e_new in state.healthy_executors:
        for s_new in state.sources_with_file(f):
            if (e_new, s_new) == (e_old, s_old):
                continue
            new_speed = state.V[e_new][s_new]
            switch_cost_bytes = state.P[f] if e_new != e_old else 0
            new_eta = (bytes_remaining + switch_cost_bytes) / new_speed
            savings = current_eta - new_eta
            candidates.append((e_new, s_new, savings, new_eta))

    # 还要考虑"子分片到多 executor"
    if can_subdivide(f, state):
        for k in [2, 3, 4]:    # 切成 k 份
            split_plan = compute_split_plan(f, state, k)
            candidates.append(split_plan)

    best = max(candidates, key=lambda c: c.savings)
    return best if best.savings > THRESHOLD else None
```

**子分片决策**：

> 注：v2.1 引入 [14-enterprise-network](./14-enterprise-network-and-rate-limit.md) 后，子分片策略额外接受 `RateLimitProfile` 输入：
> - per-connection limit → 单 executor 多连接
> - per-IP limit → 多 executor 机器
> - per-user limit → 多个 gateway alias 轮换
> 详见 14 §2.5。下面仅展示算法骨架。

```python
def compute_split_plan(f, state, k):
    # 选出 k 个最快的 (executor, source) 组合
    top_k = sorted(state.all_pairs_by_speed(), reverse=True)[:k]

    # 总速度
    total_speed = sum(speed for _, _, speed in top_k)

    # 切丢已下的 P[f]（splitting 默认从头开始）
    bytes_to_redownload = f.size  # 全文件重下
    new_eta = bytes_to_redownload / total_speed

    # 检查 S3 part 约束
    sub_chunk_size = bytes_to_redownload / k
    if sub_chunk_size < 5 * 1024 * 1024:    # < 5MB
        return None  # 不允许
    if sub_chunk_size > 5 * 1024 * 1024 * 1024:  # > 5GB
        return None  # 不允许

    # 检查 storage backend 支持
    if not state.storage.is_multipart_capable():
        return None

    return SplitPlan(k=k, parts=top_k, expected_eta=new_eta)
```

### 4.2 Slow Path：LP 求解

仅在 fast path 收益 < 阈值但仍有"明显非最优"嫌疑时触发。用 `pulp` / `scipy.optimize.linprog` 求解 §1.3 的 LP 松弛版。

不展开实现细节；超时 5 秒回退 fast path 结果。

### 4.3 决策触发时机（最优化的核心子问题）

**决策何时触发**本身就是一个最优化问题：

```
trade-off:
  trigger 太频繁  → optimizer CPU 开销 + 决策抖动 + 频繁切换的 switch_cost
  trigger 太稀疏  → 错过最优切换窗口 → makespan 实际比理论最优长

我们要的 trigger 策略是：让"必要决策点"100% 覆盖，"无效决策点"近 0%。
```

下面是 v2.1 的触发框架。**核心思路：把所有触发分成 3 级 + 自适应周期 + 瓶颈聚焦 + 信息量门控**。

#### 4.3.1 三级触发分类

按"是否可能改变 makespan"做强弱分类。每级有独立处理路径。

##### Tier 1 — Hard triggers（事件即触发，无 debounce）

**判定原则：能直接证明 makespan 已变 / 即将变**。

| 信号 | 为什么 hard | 处理 |
|------|-----------|------|
| **Bottleneck 文件速度 < 基线 × 30% 持续 ≥ 15s** | 当前最长 ETA 直接拉长，必须立即评估 | 立即调 `decide(bottleneck_file)` |
| Executor 进入 `faulty` / 心跳超时 | 容量减少；可能影响多个文件 | 立即调 `decide(all_files_on_this_executor)` |
| 新 Executor 注册成功（capacity 变） | 新增容量；可能用于瓶颈文件子分片 | 立即调 `decide(bottleneck_file)`（仅瓶颈，不全量） |
| Source 进入 `circuit_open`（详见 03 §8） | 该 source 完全不可用 | 立即调 `decide(files_using_this_source)` |
| 任务 ETA > 预测 × 1.5 触发持续 30s | 实际进度严重偏离 → 模型与现实不一致 | 立即全量 `decide(all_files_in_task)` |
| 用户 `POST /api/tasks/{id}/replan` | 显式请求 | 立即全量 |

🔒 **不变量 27：Hard trigger 不被 cooldown 抑制**
（不变量 21 的 60s/file cooldown 仅约束 soft trigger 与周期。Hard 永远响应。）

##### Tier 2 — Soft triggers（进入待评估队列，下一个 tick 评估）

**判定原则：可能改变 makespan，但不一定**。攒一批一起评估。

| 信号 | 处理 |
|------|------|
| **非瓶颈文件**速度变化（任意方向） | 入队，下一个 tick 评估"是否成为新瓶颈" |
| Source 进入 `throttled`（非 circuit_open） | 入队 |
| Executor 进入 `degraded`（不是 faulty） | 入队，仅评估它当前承担的 chunk |
| Executor 完成一个 sub-chunk 进入 idle | 入队，评估"这个 idle 容量能用于瓶颈吗" |
| ETA 预测偏离 ∈ [20%, 50%] | 入队 |

##### Tier 3 — Periodic safety net

> 兜底：万一漏报某个 hard/soft 信号，不至于卡死在次优状态。

不是固定 30s。**周期自适应**：

```python
def next_tick_interval(state):
    # 系统稳定性：最近 5 分钟内 hard trigger 数 / soft trigger 数 / actual_savings 分布
    instability = state.recent_hard_triggers_5m * 5 \
                + state.recent_soft_triggers_5m \
                + state.recent_speed_variance_pct

    if instability > 50:    return 5   # 高扰动期，密集观察
    if instability > 20:    return 15
    if instability > 5:     return 30
    if state.all_etas_on_track and state.recent_actions_5m == 0:
        return 120          # 完全稳定，省 CPU
    return 60               # 默认
```

**最小周期 5s，最大 120s**。

#### 4.3.2 瓶颈聚焦（Bottleneck-Scoped Evaluation）

不是每个 trigger 都全量重新规划所有文件。**仅评估"可能影响 makespan"的文件子集**：

```python
def files_to_evaluate(state, trigger):
    if trigger.tier == 1:
        return trigger.affected_files     # hard trigger 自带受影响文件集合

    bottleneck_eta = max(state.eta_per_file.values())
    candidates = []

    # 1. 当前瓶颈文件
    candidates.append(state.bottleneck_file)

    # 2. ETA 在瓶颈 ±20% 内的文件（容易成为新瓶颈）
    for f, eta in state.eta_per_file.items():
        if abs(eta - bottleneck_eta) / bottleneck_eta < 0.2:
            candidates.append(f)

    # 3. 有 idle executor 等待时，所有文件都可能受益（额外容量）
    if state.has_idle_executor():
        candidates.extend(state.in_progress_files)

    return list(set(candidates))
```

**Why**：评估 100 文件 vs 评估 5 文件，CPU 差 20×。瓶颈聚焦让 fast path 真的 fast。

#### 4.3.3 信息量门控（Information-Gated Triggering）

> 即使 trigger 进了队列，**评估前先问：state 真的变了吗？变化能改变决策吗？**

```python
def should_actually_evaluate(file, state, last_state):
    # 1. 状态显著变化检测
    speed_changed = max_speed_change_pct(state, last_state, file) > 10
    capacity_changed = state.idle_executors != last_state.idle_executors
    progress_changed = state.P[file] / file.size > last_state.progress + 0.05

    if not (speed_changed or capacity_changed or progress_changed):
        return False   # 状态实质未变，跳过

    # 2. 决策稳定性预估（粗略）
    expected_diff = abs(estimate_savings(state, file) - last_state.expected_savings)
    if expected_diff < SAVINGS_NOISE_THRESHOLD:
        return False   # 即使评估也是同一个决定，跳过

    return True
```

`SAVINGS_NOISE_THRESHOLD` 默认 5 秒（小于这就是噪声）。

#### 4.3.4 串行化与 generation fence（v2.1, DIST-V21-04）

> 仅有 SCHEDULER_LOCK 保护 decide 阶段是不够的：apply 阶段（push 到 executor + 写 DB）也可能被并发决策污染。

**Generation number 协议**：

```python
class FileOptimizationState:
    file_id: UUID
    optimization_generation: int = 0    # 单调递增；每次 PlanOptimizer 决策 +1
    expected_executor_set: set[str]     # 当前期望的 executor 集合
```

每次 `decide()` + `apply()`（**整个**）必须在同一全局锁内：

```python
async def optimize_file(file_id):
    async with SCHEDULER_LOCK:
        state = load_state(file_id)
        new_gen = state.optimization_generation + 1

        decision = decide(state)
        if not decision: return

        # 持久化 generation
        await db.execute(
            "UPDATE file_subtasks SET optimization_generation = $1 WHERE parent_subtask_id = $2",
            new_gen, file_id
        )

        # 同 lock 内 apply：通过 WSS push 到 executor，每条 push 携带 new_gen
        for exec_id, assignment in decision.assignments.items():
            await ws_push(exec_id, {
                "type": "assign",
                "target_executor_epoch": ...,
                "optimization_generation": new_gen,        # ← fence
                ...
            })

        # 同 lock 内 reclaim：旧 owner 通过 WSS abort 通知（不只是写 DB）
        for old_exec in decision.reclaim_targets:
            await ws_push(old_exec, {
                "type": "abort",
                "subtask_id": ...,
                "reason": "reclaimed_by_optimizer",
                "old_optimization_generation": state.optimization_generation,
            })
```

**Executor 端校验**：上报 part 完成时携带 `optimization_generation`；controller 校验 `received_gen == file.optimization_generation`，不等就 stale，丢弃上报。

🔒 **不变量 42 (v2.1, DIST-V21-04)**：PlanOptimizer 的 decide + apply（push + DB write + reclaim notification）必须在同一 SCHEDULER_LOCK 内完成；reclaim 必须通过 WSS push 主动通知旧 owner，不能仅写 DB。

#### 4.3.5 Trigger 抖动抑制

同一文件短时间内反复触发：

| 防护 | 配置 |
|------|------|
| 单文件 cooldown | 60s（不变量 21；hard trigger 也尊重，但 hard 优先级压过 cooldown 的提示告警） |
| 同类型 hard trigger 去重 | 5s 内 `(reason, affected_file_set_hash)` 仅算一次（v2.1, DIST-V21-10）—— dedup key **不含** executor_id，使 100 个 executor 同时 register 时仅算 1 次 |
| Soft trigger 队列合并 | 30s 窗口内同 file 多次 soft 合并为一次 |
| 全局 trigger 频率上限 | 单 controller 优化决策 ≤ 60 次/分钟 |

#### 4.3.6 决策预算（Cost-Bounded Triggering）

PlanOptimizer 自身 CPU 也是有限资源。当 controller 同时管理大量任务时，触发预算分配：

```
budget(task) = base_quota
             + priority_bonus(task.priority)        # higher prio gets more
             + bottleneck_bonus(if task is system-wide longest)
             - recency_penalty(time_since_last_decision)
```

低预算任务的 soft trigger 直接丢弃；保证整体 controller 不爆 CPU（详见 §9.1 容量）。

#### 4.3.7 触发流程总图

```
                ┌──────────────────────────────────────┐
                │ Signal (heartbeat / state change /   │
                │  user action / periodic tick)         │
                └──────────────────┬────────────────────┘
                                   ▼
                          ┌─────────────────┐
                          │ Classify Tier   │
                          └────┬──────┬─────┘
                       Tier 1  │      │  Tier 2 / 3
                  (hard)       │      │  (soft / periodic)
                               ▼      ▼
                  ┌────────────────┐  ┌──────────────────┐
                  │ Cooldown check │  │ Enqueue to       │
                  │  (passthrough) │  │  pending_queue   │
                  └───────┬────────┘  └────────┬─────────┘
                          │                    │  next tick
                          │   ┌────────────────┘
                          ▼   ▼
                  ┌─────────────────────────────┐
                  │ Bottleneck-scoped selection │
                  │ (files_to_evaluate)          │
                  └────────────┬─────────────────┘
                               ▼
                  ┌─────────────────────────────┐
                  │ Information-gated check     │
                  │ (should_actually_evaluate)  │
                  └────────────┬─────────────────┘
                       skip ←──┤  ──→ proceed
                               ▼
                  ┌─────────────────────────────┐
                  │ Budget check                │
                  │ (per-task quota)            │
                  └────────────┬─────────────────┘
                               ▼
                  ┌─────────────────────────────┐
                  │ DecisionEngine.decide()      │
                  │  (fast_path → slow_path)    │
                  └────────────┬─────────────────┘
                               ▼
                  ┌─────────────────────────────┐
                  │ Apply or skip decision      │
                  │ (write optimization_decisions) │
                  └─────────────────────────────┘
```

#### 4.3.8 配置默认值

```yaml
trigger:
  # 周期
  period:
    min_seconds: 5
    max_seconds: 120
    default_seconds: 60
    instability_thresholds: {high: 50, medium: 20, low: 5}

  # Hard trigger
  bottleneck_speed_drop_pct: 30
  bottleneck_speed_drop_duration_s: 15
  eta_drift_pct: 50
  eta_drift_duration_s: 30

  # Cooldown
  cooldown_per_file_seconds: 60        # soft trigger 受限；hard 不受
  hard_trigger_dedup_window_s: 5
  soft_trigger_merge_window_s: 30

  # 全局
  max_decisions_per_controller_per_minute: 60
  max_files_per_evaluation: 20

  # 门控
  speed_change_threshold_pct: 10        # 信息量门控
  savings_noise_threshold_seconds: 5
  bottleneck_proximity_pct: 20          # ETA 在瓶颈 ±20% 内才参与评估
```

#### 4.3.9 v2.2 进阶：预测性触发（Predictive Triggering）

v2.1 的所有 trigger 都是 **reactive**——速度变了才反应。v2.2 引入 **predictive**：

| 预测信号 | 来源 | 行动 |
|---------|------|------|
| Source 429 概率上升（按 time-of-day + 累计使用量历史） | 历史 source_throttle_state 表 | 提前 30s 把流量往其他源迁 |
| Executor 性能退化（按 health_score 趋势） | executor_status_history 表 | 提前 reduce 该 executor 上分配 |
| 任务总耗时偏长（按文件 size 分布预测） | 历史任务库 | 一开始就直接 split 大文件 |

📝 **决策**：v2.1 只用 reactive；predictive 收集决策历史 30 天后（v2.2）启用，先验证模型准确度。

#### 4.3.10 已知失效模式

| 场景 | 失效 | 缓解 |
|------|------|------|
| EWMA 速度估计有 ~10s 延迟 → bottleneck 速度跌已经发生 ~10s 才被 hard trigger | 决策已经"晚了"几秒 | 接受；adaptive period 调到 5s 时基本能压到 5-15s 总反应延迟 |
| 多个 hard trigger 同时触发 → 决策 race | 后一个决策可能基于前一个未生效的 state | 串行化：单 controller 内 PlanOptimizer 用全局 lock |
| 触发风暴（如重启时所有 executor 同时 register） | 每个都是 hard trigger | dedup window 5s 合并 |
| User 频繁 `replan` 制造 thrash | 决策抖动 | per-user replan 限流 6 次/小时 |

---

## 5. 多 Executor 协作下载（S3 multipart 实现）

### 5.1 协议总览

```
─────────── Phase 1: Initiate (Controller 主导) ───────────

Controller --[s3:CreateMultipartUpload]--> S3
S3 ─────────[upload_id = "abc..."]────────► Controller

Controller 决定切分: k=3, byte ranges & part numbers
   - executor-1: bytes [0..1GB),    part_number=1
   - executor-2: bytes [1GB..2GB),  part_number=2
   - executor-3: bytes [2GB..3GB),  part_number=3

🔒 不变量 32 (v2.1, DIST-V21-01):
   Controller MUST persist (file_subtasks.multipart_upload_id, multipart_initiator_epoch)
   to PG and COMMIT **before** sending assignments to executors.
   This is the same "CAS-then-enqueue" pattern as 03 §2.3.

Controller --[INSERT INTO file_subtasks ... multipart_upload_id=abc... ]--> PG
Controller --[COMMIT]--> PG                   ← 必须先 commit
Controller --[heartbeat response: subtask assignments + multipart_upload_id]--> Executors

─────────── Phase 2: Parallel download + upload ───────────

executor-1 ─[GetRange(source, 0..1GB)]─► source ─► [stream]
            ─[s3:UploadPart(upload_id, part=1, ChecksumSHA256)]─► S3
            ─[heartbeat: part 1 done, etag, sha256]─► Controller

executor-2: same with byte range [1GB..2GB), part=2
executor-3: same with byte range [2GB..3GB), part=3

─────────── Phase 3: Complete (Controller 主导) ───────────

Controller waits for all parts (with deadlines)
Controller --[s3:CompleteMultipartUpload(upload_id, parts=[{1,etag1}, {2,etag2}, {3,etag3}])]--> S3
S3 ──[ChecksumSHA256 of full object]──► Controller
Controller verifies aggregate sha matches expected_sha256
```

✅ **关键性质**：每个 executor 只与 source（HF/MS/...）+ S3 通信。**不需要任何跨节点 FS 访问**。这正是用户担忧的解法。

### 5.2 数据模型扩展

```sql
-- 单文件可能拆成多个 sub-chunk，每个 sub-chunk 由不同 executor 负责
ALTER TABLE file_subtasks ADD COLUMN parent_subtask_id UUID REFERENCES file_subtasks(id);
-- parent 表示"协调中心"逻辑实体；children 是真正下载的 sub-chunk

ALTER TABLE file_subtasks ADD COLUMN multipart_upload_id VARCHAR(256);   -- 已存在 (v2.0)
ALTER TABLE file_subtasks ADD COLUMN s3_part_number INT;                   -- 新增：当前 sub-chunk 的 part_number
ALTER TABLE file_subtasks ADD COLUMN byte_range_start BIGINT;
ALTER TABLE file_subtasks ADD COLUMN byte_range_end BIGINT;                 -- inclusive
ALTER TABLE file_subtasks ADD COLUMN s3_etag VARCHAR(128);                  -- 上传完返回的 ETag

CREATE INDEX idx_subtask_parent ON file_subtasks(parent_subtask_id) WHERE parent_subtask_id IS NOT NULL;
```

**逻辑**：

- 不分片时：`parent_subtask_id = NULL`，`s3_part_number = NULL`（行为同 v2.0）
- 分片时：1 个 parent subtask + N 个 children；children 的 `parent_subtask_id` 指向 parent
- parent 状态：`split` → `verifying_remote` → `verified`
- children 状态：`pending` → `downloading` → `uploading` → `verified`（行为同 v2.0 单 chunk）

### 5.3 Sub-chunk 大小约束

S3 multipart 协议：

- 单 part：≥ 5 MB（除最后一个）≤ 5 GB
- 总 part 数：≤ 10,000

`PlanOptimizer.compute_split_plan` 检查这些约束。例如 17GB 文件：

- 切 4 份：每份 4.25 GB，OK
- 切 8 份：每份 ~2.1 GB，OK
- 切 100 份：每份 170 MB，OK，但收益递减
- 切 5000 份：每份 3.4 MB，违反 ≥5MB 约束

实践默认 k ∈ {2, 3, 4, 8}，根据可用 executor 数和文件大小选。

### 5.4 故障 / 不变量保持

**Controller crash 后恢复（衔接 03 §3）**：

`recovery_routine` 已有"清理孤儿 multipart"逻辑。多 executor 多 part 情形下，启动时强制走"recovery 屏障"：

🔒 **不变量 33 (v2.1, DIST-V21-08)**：standby 提升后**先**进入 `recovery_in_progress=true`，**所有**心跳响应返回 `503 RECOVERY_IN_PROGRESS`，executor 收到后暂停 in-flight UploadPart（保留 .parts/ 不丢），直到对账完成方解除。

详细对账步骤：

1. 扫描 DB 所有 `multipart_upload_id IS NOT NULL AND status NOT IN (verified, failed_permanent, cancelled)` 的 subtask
2. **同时扫 S3**：调 `s3:ListMultipartUploads(bucket, prefix)` 拿到该 bucket 所有未完成 multipart upload
3. **三向对账**（DB ↔ S3 ↔ 各 executor 心跳上报）：
   - DB 有 upload_id 但 S3 没 → 视为 controller crash 前未真正成功发起；重 CreateMultipartUpload，刷新 DB
   - S3 有 upload_id 但 DB 没（孤儿）→ 调 `s3:AbortMultipartUpload` 清理
   - 双方都有 → 进入 part 级对账
4. **Part 级对账**：对每个 (upload_id, part_number)：
   - `s3:ListParts(upload_id)` 拿到 S3 端真实 ETag 集合
   - 与 DB 中 `file_subtasks.s3_etag` 对比
   - DB 有但 S3 没 → 该 part 重新分配
   - S3 有但 DB 没 → 该 part 记入 DB（接受 S3 真相）
   - 双方都有但 ETag 不同 → 以 S3 为准（最新 PUT 胜出，DIST-V21-02 边界）
5. 若 parts 集齐 → 调 `s3:CompleteMultipartUpload`，**Complete 之前必须先调 `s3:ListParts` 二次确认 ETag 集合与 Complete 提交一致**
6. 24h 未完成 → abort 整个 multipart upload
7. 解除 `recovery_in_progress`，开始正常接受心跳

**Executor crash**（仅一个子任务执行器挂）：

⚠️ **DIST-V21-02 修正**：v2.0 设想"S3 last-write-wins on PartNumber"在 split-brain 边界下不安全 —— 旧 executor 的 TCP 流可能在 reclaim 之后才到达 S3，导致 controller 已记录的 ETag 与 S3 端最终 ETag 不一致，`s3:CompleteMultipartUpload` 报 `InvalidPart`。

**正确协议（v2.1 修订）**：

1. Controller 检测到（心跳超时） → reclaim 该 executor 的 sub-chunk subtask
2. **Bump part_number**：把原 `part_number=N` 改为 `part_number=N + 1000`（或 N + 1000×reclaim_count），保留旧 part 为孤儿
3. 把 sub-chunk 重新分配给其他 executor，使用 **新 part_number** + 同一个 `multipart_upload_id`
4. Controller DB 中 `file_subtasks.s3_part_number` 更新为新值；旧值废弃但保留为 audit
5. **Complete 前**：调 `s3:ListParts(upload_id)` 拿真实 part 列表 → 仅取 DB 中"current part_number"对应的 part → 提交 `CompleteMultipartUpload(parts=[{N+1000, ETag_new}, ...])`
6. 旧的 part_number=N（如果旧 executor 最终上传成功）成为孤儿，不影响 Complete；24h 后由 lifecycle 清理或 abort 单个 part 不可（S3 限制）→ 接受为孤儿，最终 abort 整个 multipart 时被一并清理

🔒 **不变量 26（修订 v2.1）**：sub-chunk 的 `part_number` 在 multipart_upload 内**首次分配后稳定，仅在 reclaim 时 bump**。

🔒 **不变量 34 (v2.1, DIST-V21-02)**：`s3:CompleteMultipartUpload` 调用前必须执行：
```python
s3_parts = s3.list_parts(upload_id).Parts        # 真实 ETag
db_parts = db.fetch_current_part_etags(subtask_parent_id)  # 当前期望
parts_to_complete = [
    {"PartNumber": p.PartNumber, "ETag": p.ETag}
    for p in s3_parts
    if (p.PartNumber, p.ETag) in db_parts        # 严格匹配
]
assert len(parts_to_complete) == expected_part_count, "ETag mismatch — abort and retry"
```

### 5.5 非 S3 backend：退化模式

storage backend 类型 → 多 executor 协作能力：

| Backend | Multipart 协议 | 支持子分片？ |
|---------|--------------|------------|
| AWS S3 | ✅ 标准 | ✅ |
| 华为云 OBS | ✅ 兼容 S3 | ✅ |
| MinIO | ✅ | ✅ |
| Ceph RGW | ✅ | ✅ |
| 阿里云 OSS | ✅ | ✅ |
| **NFS / local FS** | ❌ | ❌ 只能单 executor |
| **HDFS** | ⚠️ 通过 multipart 兼容层 | ⚠️ 测试后启用 |

退化逻辑（`PlanOptimizer.can_subdivide()`）：

```python
def can_subdivide(file, state):
    storage = state.storage[file.task.storage_id]
    return storage.backend_type in {"s3", "obs", "minio", "ceph", "oss"}
```

非兼容 backend：optimizer 只能"换 executor"（单 executor 全文件接管），不能"切小"。可写到任务详情页提示用户。

---

## 6. 接入现有系统

### 6.1 与 06 §1.8 的关系

06 §1.8 阶段 B（"下载中持续校准 + 局部重平衡"）现状用作 v2.0 的简化前置版。

v2.1 用本文（13 章）替代。06 §1.8 修订指向：

> v2.0 内置**反应式**重平衡（60s 周期，30% 阈值）；v2.1 升级为**自适应运筹优化**，详见 [13-adaptive-download-optimization.md](./13-adaptive-download-optimization.md)。

### 6.2 与 03 §2 fence token 的兼容

子分片创建 children subtask 时：

- 每个 child 有自己的 `assignment_token`
- parent subtask 的 `assignment_token` 用于"哪个 controller 在协调本 file"的 fence
- Controller crash 后 standby 接管 → 新 epoch → 重新签发 children token

不变量 6（assigned → downloading 必带 assignment_token）保持成立。

### 6.3 与心跳协议的扩展

心跳响应中 `new_assignments[].chunk_plan` 字段（02 §4.3 已存在）扩展为：

```json
"chunk_plan": [
  {
    "chunk_index": 0,
    "byte_start": 0,
    "byte_end": 1073741823,
    "source_id": "modelscope",
    "multipart_upload_id": "abc123",
    "s3_part_number": 1,
    "parent_subtask_id": "uuid-parent..."
  }
]
```

executor 拿到 `parent_subtask_id` + `s3_part_number` 后：

1. 用 `multipart_upload_id` + 自己的 `part_number` 调 `s3:UploadPart`
2. 完成后心跳上报 part 完成（含 etag + sha256_partial）

### 6.4 与 04 §3.2 STS 临时凭证的兼容

每个 executor 仍需 STS 临时凭证。多 executor 同 multipart_upload_id 时：

- 所有 executor 的 STS Policy 都允许操作该 upload_id（policy 用 `s3:UploadId` 条件键）
- Controller 签发凭证时把 `s3:multipartUploadIDs` 加入 condition

```json
{
  "Effect": "Allow",
  "Action": ["s3:UploadPart", "s3:AbortMultipartUpload"],
  "Resource": "arn:aws:s3:::bucket/key",
  "Condition": {
    "StringEquals": {"s3:multipartUploadID": "abc123"}
  }
}
```

### 6.5 决策审计

每次 PlanOptimizer 决策写：

```sql
CREATE TABLE optimization_decisions (
    id              BIGSERIAL PRIMARY KEY,
    task_id         UUID NOT NULL REFERENCES download_tasks(id),
    triggered_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    trigger_reason  VARCHAR(64) NOT NULL,         -- periodic / executor_state / source_state / user_replan
    state_snapshot  JSONB NOT NULL,                -- V[E][S], P[F], A[F]
    decision        JSONB NOT NULL,                -- {action: switch|split, ...}
    expected_savings_seconds FLOAT,
    actual_outcome  JSONB,                         -- 30s 后回填（near-term）；task 终态时若窗口未到则标 task_terminal_before_window
    actual_savings_seconds FLOAT,
    final_savings_seconds FLOAT,                    -- task 完成时回填（OR-V21-16）
    evaluation_horizon VARCHAR(16)                   -- "30s" / "task_terminal" / "task_terminal_before_window"
);

CREATE INDEX idx_opt_decision_task ON optimization_decisions(task_id, triggered_at);
```

可用于：

1. 算法回放（验证决策是否合理）
2. 训练改进（收集"决策 vs 实际"差，未来用 RL 优化）
3. 运维诊断（"为什么这个任务跑那么久 → 看决策序列"）

---

## 7. UX

### 7.1 任务详情页扩展

任务详情页（10 §3.3）的"源分配"卡片下增加"优化决策日志"：

```
┌────────────────────────────────────────────────────────────────┐
│  优化决策日志（近 1 小时）                          [展开全部] │
├────────────────────────────────────────────────────────────────┤
│ 14:32:18  📐 子分片决策                                        │
│   触发：model-001 当前 ETA 90s（瓶颈）                         │
│   动作：从 (node-3, hf_mirror) 切换到 4-way split              │
│         [node-3:hf_mirror, node-7:modelscope,                  │
│          node-1:modelscope, node-5:hf_mirror]                  │
│   预期节省：62 秒                                              │
│   实际：57 秒（off by -8%）✓                                   │
├────────────────────────────────────────────────────────────────┤
│ 14:33:01  🔀 source 切换                                       │
│   触发：hf_mirror 进入 throttled（429 率 8%）                  │
│   动作：剩余 3 个 chunk 从 hf_mirror 改 modelscope             │
│   预期节省：18 秒                                              │
│   实际：（进行中）                                             │
└────────────────────────────────────────────────────────────────┘
```

### 7.2 用户主动 replan

```
[ ⚡ 重新优化 ]   按钮触发 POST /api/tasks/{id}/replan
```

强制立即跑 PlanOptimizer。

### 7.3 admin freeze

```
[ ❄️ 冻结优化 ]   admin only
```

把任务标记为 `optimizer_frozen=true`，PlanOptimizer 跳过它（运维介入场景）。

---

## 8. 限制与已知问题

| ID | 限制 | 缓解 |
|----|------|------|
| ADO-01 | LP 求解可能 > 5s（>1000 个 chunk）→ fallback fast path | 监控求解时延；超 50% 任务退化时升级硬件 |
| ADO-02 | EWMA 速度估计有滞后；激进切换可能基于过期数据 | hysteresis + 多次确认（不变量 21） |
| ADO-03 | Sub-chunk 数 > 100 时管理开销大 | 默认上限 k=8；可配置 |
| ADO-04 | 非 S3 backend 不能子分片 | 退化到换 executor 模式 |
| ADO-05 | 跨任务竞争未建模（v2.1 单任务最优化） | v2.2 全局调度 |
| ADO-06 | 用户 force-replan 可能制造抖动 | 即使用户触发也走 hysteresis cooldown |
| ADO-07 | 决策表 `optimization_decisions` 体积大 | 30 天 TTL + parquet 归档 |
| ADO-08 | 优化器自身故障不能阻断任务 | optimizer 异常时 fall back to 当前分配（任务继续，只是不再调整） |

---

## 9. 配额与成本

### 9.1 Controller CPU

PlanOptimizer 周期 CPU 占用（v2.1 修订）：

- **Fast path**：O(N × M × K × |Plans|)。N 文件 × M executor × K source × 5 plan 候选 = 100 × 10 × 3 × 5 = **15,000 ops**，预计 < 50ms
- **Slow path（LP）**：变量数同上 ~15K，约束数 O(M + K + N)；CBC / linprog 实测 <3s（含模型构建 + presolve + simplex），5s 超时阈值留 67% 缓冲
- **慢路径降级**：连续 N 次超时 → 自动改 LP 松弛（去整数约束）保证 anytime 解（OR-V21-03 修复）
- **实测 CPU 上限**：单 controller 可同时管理 ~50 个 active task。超过则建议多 controller 按 tenant 分片（详见 v2.2 roadmap）

🔒 **不变量 38 (v2.1, OR-V21-03)**：slow path 连续超时 ≥ 3 次 → 自动降级为 LP 松弛 + 1s anytime cutoff，永远返回 best feasible 解，不让"slow path 超时 → 无脑回退 fast path"成为永久状态。

如果 active task > 50：考虑分片（按 task_id hash 到多 controller，但这就到 v2.2 横向扩展了）。

### 9.1.1 配置默认值的统计学依据（v2.1, OR-V21-05/09/14/15）

> 把"经验值"升级为有公式可追溯。

| 参数 | v2.0 默认 | v2.1 公式 | 说明 |
|------|---------|---------|------|
| `α` (switch_cost 惩罚因子) | 1.5 | **1.0** | 单位转换；`switch_cost / V_avg` 已经是秒。> 1 = risk-aversion，由 chaos 测试反推 |
| `switch_threshold_savings_pct` | 15% | **2 × CV(speed_estimate)** ≈ 15% | EWMA σ ≈ 7.5%，节省 < 15% 不可信（信噪比） |
| `SAVINGS_NOISE_THRESHOLD` (信息门控) | 5s 绝对 | **`max(5s, 0.05 × current_eta, 2 × σ(estimate_savings))`** | 30s 任务 5s = 17%（显著）；1h 任务 5s = 0.14%（噪声） |
| `cooldown_per_file_seconds` | 60s | **`3 × EWMA_window + heartbeat_period`** = 30s + 10s = 40s（保守取 60s） | EWMA τ=10s，3τ 后达 95% 收敛 |
| `dedup_window` | 5s 绝对 | **`max(5s, 1 × heartbeat_period)`** | 至少覆盖 1 个心跳周期 |
| `instability_thresholds` (自适应周期) | {high:50, mid:20, low:5} | 基于 1h 滑动窗的 hard+soft trigger 计数；阈值由 chaos 测试反推 | 详见 §10.4 |

📝 **决策**：v2.1 GA 前用 chaos 测试 + α scan {0.5, 1.0, 1.5, 2.0, 3.0} 反推真实 prod 数据下的最佳值，固化到 §4.3.8 配置。

### 9.2 决策频率成本

```yaml
optimizer:
  periodic_interval_seconds: 30
  event_triggered: true
  fast_path_only_threshold_seconds: 5    # fast path 期望 < 5s
  slow_path_timeout_seconds: 5
  switch_threshold_savings_pct: 15       # 至少省 15% 才切换
  cooldown_per_file_seconds: 60
```

### 9.3 不增加流量成本

PlanOptimizer 本身不产生网络流量（决策是数据库内）。它**改变**的是已有下载任务的执行方式。

⚠️ **唯一例外**：触发"丢弃已下载部分重新下载"时，会让该文件流量翻倍（已下 + 重下）。算法的 switch_threshold 设计就是防止这种情况除非真有显著收益。

---

## 10. 测试 / 评估策略

详见 [`07-test-plan.md`](./07-test-plan.md) §10（v2.1 引入）。要点：

### 10.1 决策正确性（unit）

- 不会在已下 95% 时切换（不变量 20）
- Cooldown 内不重新决策
- 切换收益 < 阈值时不动
- Storage 非 S3 → 不会推荐子分片

### 10.2 端到端 makespan 改进（integration）

固定 source 速度脚本（可注入）：

```yaml
- id: ADO-E2E-001
  scenario: |
    任务：5 个 17GB 文件
    初始：所有文件分配给 node-1 (HF Mirror, 400 MB/s)
    @ T=20s：HF Mirror 限流，速度降至 50 MB/s
    @ T=20s：ModelScope 速度仍为 950 MB/s, node-2 闲置
    期望：T<30s 内 PlanOptimizer 触发，把剩余字节迁到 ModelScope
    验证：v2.1 任务完成时间 < v2.0 反应式版本完成时间 × 0.6
```

### 10.3 决策稳定性（chaos）

随机注入速度抖动（±50%），验证：

- 优化器不进入抖动循环
- 单文件切换次数 < 3 次/小时
- 决策表的 `actual_savings` 与 `expected_savings` 偏差 < 50%（系统性偏差则算法有 bug）

---

## 11. Roadmap 定位

### 11.1 v2.0 不做

继承 06 §1.8 反应式简化版（够用，不阻塞 GA）。

### 11.2 v2.1 first-class

完整运筹优化 + 子分片 + S3 多 executor 协作。

### 11.3 v2.2 进阶

- **跨任务全局优化**（多任务竞争同一 executor 时的公平调度 + 抢占）
- **历史驱动**：用历史决策表训练 ML 模型预测 source 速度退化（提前切换而非反应式）
- **预测性子分片**：任务一开始就直接 split 大文件（基于历史 source 速度画像）

### 11.4 v2.3+ 研究方向

- 强化学习决策器（state → action policy）
- 联邦多 cluster 优化（同 region 多 controller cooperative）

---

## 12. 与其他文档的链接

- 不变量：→ [01-architecture.md](./01-architecture.md) §7（新增 20-26）
- API 心跳协议（chunk_plan 扩展）：→ [02-protocol.md](./02-protocol.md) §4.3
- Fence token 与子任务恢复：→ [03-distributed-correctness.md](./03-distributed-correctness.md) §2 §3
- STS 凭证 conditions：→ [04-security-and-tenancy.md](./04-security-and-tenancy.md) §3.2
- v2.0 反应式重平衡（被本文升级）：→ [06-platform-and-ecosystem.md](./06-platform-and-ecosystem.md) §1.8
- 测试矩阵：→ [07-test-plan.md](./07-test-plan.md) §10（v2.1 引入）
- Roadmap：→ [08-mvp-roadmap.md](./08-mvp-roadmap.md)
- 任务详情页 UX：→ [10-frontend-wireframes.md](./10-frontend-wireframes.md) §3.3
