# 06 — 平台能力与生态

> 角色：让系统从"内部工具"成为"平台"。重点：多源调度、增量下载、CLI/SDK、与训练/推理生态集成。
> 取代：v1.0 §7 UI 部分、v1.4 §6 / §10 / §11、v1.5 §2.2-2.3、§3。

---

## 0. 从历史文档迁移指引

| 旧位置 | v2.0 位置 |
|--------|----------|
| `design_document_review_and_e2e.md` §2.1 hf_transfer 借鉴 | 本文 §1.6 + 03 §6 |
| `design_document_review_and_e2e.md` §3 多执行器特性 | 01 §5.3 + 本文 §6 |
| `design_document_fault_tolerance_and_visualization.md` §10 定时探查与自动下载 | 本文 §3.4 |
| `design_document_fault_tolerance_and_visualization.md` §11 模型完整文件清单策略 | 本文 §3.5 |
| `design_document_fault_tolerance_and_visualization.md` §6 UI 详细设计 | 本文 §7 |

---

## 1. 多源下载与负载均衡（v2.0 头号特性）

### 1.1 动机

- HF 直连在中国境内不可用，单源国内体验差
- 多源并行可逼近"出口带宽总和"而非单源带宽
- 单源 SLO 抖动时，自动 failover 比重试更快
- 用户内网常自托管模型镜像，应作为最优先源

### 1.2 内置源清单

v2.0 内置以下 6 个源驱动（`SourceDriver` 接口）。控制器启动时按配置文件 `sources.yaml` 装配。

| Driver ID | 域名 | 适用 | 名字映射 | 默认开启 |
|-----------|------|------|---------|---------|
| `huggingface` | huggingface.co | 全球，权威 SHA256 来源 | identity | ✅ |
| `hf_mirror` | hf-mirror.com | 中国境内 HF 代理 | identity | ✅（中国时区/region 时） |
| `modelscope` | modelscope.cn | 中国主流 LLM | 见 §1.5 | ✅（中国时区/region 时） |
| `wisemodel` | wisemodel.cn | 中文 LLM 备份源 | 见 §1.5 | ⚙️ 默认关闭，可启用 |
| `opencsg` | opencsg.com | 中文 LLM 备份源 | 见 §1.5 | ⚙️ 默认关闭 |
| `s3_mirror` | （用户配置） | 内网/私有 S3 mirror | 自定义前缀 | ⚙️ 按租户启用 |

📝 **决策**：不内置 Civitai / Ollama / GitHub Releases —— 覆盖率太低或格式不通用；通过插件机制（§1.10）支持。

🔒 **不变量 11：HF 永远是 SHA256 真值来源**
所有其他源下载的内容，最终必须比对 HF 给出的 expected_sha256。失败则该 (source, file) 组合进入 5min 黑名单。

### 1.3 SourceDriver 抽象接口

```python
class SourceDriver(Protocol):
    id: str                           # "huggingface" / "modelscope" / ...
    domain: str                       # 用于域名 pin

    async def resolve(
        self, repo_id: str, revision: str,
    ) -> SourceManifest | None:
        """
        返回该源是否覆盖此 repo+revision；如有，返回 file 列表。
        None 表示该源不支持此 repo（或不存在此 revision）。
        """

    async def download_range(
        self, file: SourceFile, byte_range: tuple[int, int],
        token: SourceToken,
    ) -> AsyncIterator[bytes]:
        """流式下载指定 byte range。"""

    async def health_check(self) -> SourceHealth:
        """探测延迟与可用性。"""

    def estimate_cost(self, bytes: int, region: str) -> Decimal:
        """估算流量成本（用于成本旋钮，详见 05 §8）。"""

@dataclass
class SourceManifest:
    source_id: str
    repo_id_in_source: str            # 该源里的 repo ID（可能与 HF 不同）
    revision_in_source: str            # 该源的 revision/commit/branch
    files: list[SourceFile]
    has_lfs_sha256: bool               # 该源是否提供官方 sha256

@dataclass
class SourceFile:
    filename: str                      # 规范化为 HF 风格 path（如 "model-00001-of-00163.safetensors"）
    size: int | None
    sha256: str | None                 # 该源给的 sha256（仅 HF/HF Mirror 给）
    download_endpoint: str             # 该源的下载 URL 或对象 key
```

### 1.4 任务模型扩展

`download_tasks` 表新增列：

```sql
ALTER TABLE download_tasks ADD COLUMN source_strategy VARCHAR(32) NOT NULL DEFAULT 'auto_balance';
-- 'auto_balance' / 'pin_huggingface' / 'pin_modelscope' / 'list:hf,modelscope' / 'fastest_only'

ALTER TABLE download_tasks ADD COLUMN source_blacklist JSONB NOT NULL DEFAULT '[]';
-- 用户主动拉黑的源 ID 列表
```

`file_subtasks` 表已有 `executor_id`，再增 `source_id`：

```sql
ALTER TABLE file_subtasks ADD COLUMN source_id VARCHAR(32);
-- 当前 chunk 实际来源；调度时填入，下载完成后写实际值
```

`subtask_chunks` 表（v2.0 新增，用于 chunk 级路由）：

```sql
CREATE TABLE subtask_chunks (
    id          BIGSERIAL PRIMARY KEY,
    subtask_id  UUID NOT NULL REFERENCES file_subtasks(id) ON DELETE CASCADE,
    chunk_index INT NOT NULL,
    byte_start  BIGINT NOT NULL,
    byte_end    BIGINT NOT NULL,        -- inclusive
    source_id   VARCHAR(32) NOT NULL,
    status      VARCHAR(16) NOT NULL,   -- pending/downloading/done/failed
    sha256_partial VARCHAR(64),         -- 该 chunk 的 sha（多线程合并校验时用）
    bytes_done  BIGINT NOT NULL DEFAULT 0,
    UNIQUE (subtask_id, chunk_index)
);
```

### 1.5 名字映射（NameResolver）

不同源对同一模型用不同 ID。比如：

| HF | ModelScope | WiseModel | OpenCSG |
|----|-----------|-----------|---------|
| `deepseek-ai/DeepSeek-V3` | `deepseek-ai/DeepSeek-V3` | `deepseek-ai/DeepSeek-V3` | `deepseek-ai/DeepSeek-V3` |
| `Qwen/Qwen3-72B-Instruct` | `qwen/Qwen3-72B-Instruct` | `Qwen/Qwen3-72B` | `Qwen/Qwen3-72B-Instruct` |
| `meta-llama/Llama-3.1-8B` | `LLM-Research/Meta-Llama-3.1-8B` | — | — |

**Resolver 三层兜底**：

1. **直接 identity**：组织名/模型名相同（80% 的 HF 模型）
2. **规则映射**：内置规则表（如 `meta-llama/* → LLM-Research/Meta-*`），可热更新
3. **API 反查**：调用源的 search API 用模型名搜，取 likes 最高的；缓存 24h

```yaml
# resolver-rules.yaml （内置 + 用户可覆盖）
identity_organizations: # 这些组织在所有源用相同 ID
  - deepseek-ai
  - Qwen
  - 01-ai
  - THUDM
  - baichuan-inc
  - mistralai

aliases:
  - hf: meta-llama
    modelscope: LLM-Research
    transform: "Meta-{name}"            # Llama-3.1-8B → Meta-Llama-3.1-8B
  - hf: openai/whisper-*
    modelscope: iic/speech-paraformer-* # 需要人工映射

per_model_overrides:
  - hf: "specific-org/specific-model"
    modelscope: "different-org/different-name"
```

🔒 **不变量 12**：跨源下载完成后，必须比对 HF 上对应文件的 sha256 一致；不一致则该 (source_id, repo_id, filename) 进入黑名单 24h，后续仅从 HF 下。

### 1.6 调度策略：file-level + chunk-level 双层

**File-level routing**（用于 N 个文件分配给不同源）

输入：`task.files = [f1, f2, ..., f163]`，每个 file 已知 size。
约束：每个 file 必须从单源下完整（不在文件中部切源）—— 因为流式 SHA256 依赖单线程顺序。
目标：最小化总耗时 = `max(每个源被分配的总字节 / 该源测速)`

→ 最优分配是经典「multiprocessor scheduling」LPT 启发式：

```python
def assign_files_to_sources(files: list[File], source_speeds: dict[str, float]) -> dict[File, str]:
    # source_speeds: {source_id: bytes/sec from this executor}
    # LPT: longest processing time first
    files_sorted = sorted(files, key=lambda f: -f.size)
    load = {sid: 0.0 for sid in source_speeds}  # bytes assigned
    assignment = {}
    for f in files_sorted:
        # 选当前完成时间最早的源（已分配字节 / 该源速度）
        best = min(load.keys(), key=lambda sid: (load[sid] + f.size) / source_speeds[sid])
        assignment[f] = best
        load[best] += f.size
    return assignment
```

**Chunk-level routing**（用于单文件 ≥ 100MB 时拆 chunk 给多源）

仅当文件 ≥ 100MB 且至少 2 个源都覆盖该文件时启用。

```python
def assign_chunks_to_sources(
    file_size: int, chunk_size: int, source_speeds: dict[str, float],
) -> list[ChunkPlan]:
    # 简化：按速度比例切总字节
    total_speed = sum(source_speeds.values())
    chunks = []
    offset = 0
    for sid, speed in source_speeds.items():
        portion = int(file_size * speed / total_speed)
        # 对齐到 chunk_size
        portion = (portion // chunk_size) * chunk_size
        # 最后一个源吃剩余
        ...
    return chunks
```

⚠️ **多线程 + 多源 + 流式 SHA256 不能共存**。详见 03 §6：多源 chunk 模式必须在所有 chunk 完成后做一次完整文件 SHA256（接受二次 IO 成本）。这是为什么 chunk-level 仅对超大文件启用——小文件用 file-level 单源直下，省二次扫描。

### 1.7 速度画像与失效

```sql
CREATE TABLE source_speed_samples (
    id            BIGSERIAL PRIMARY KEY,
    executor_id   VARCHAR(64) NOT NULL,
    source_id     VARCHAR(32) NOT NULL,
    measured_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    bytes_per_sec FLOAT NOT NULL,
    sample_size   BIGINT NOT NULL,                 -- 测速使用的字节数
    is_active_probe BOOLEAN NOT NULL DEFAULT FALSE  -- TRUE=主动探测，FALSE=被动观测
);
CREATE INDEX idx_speed_recent ON source_speed_samples(executor_id, source_id, measured_at DESC);

-- 老样本由后台 cron 清理（保留 7 天）
-- DELETE FROM source_speed_samples WHERE measured_at < now() - INTERVAL '7 days';
```

**测速来源**：

1. **被动**：实际下载完成时记录 `bytes / duration`
2. **主动**：每 5 分钟用 `download_range(small_file, 0..1MB)` 探测一次

**速度估计公式**：指数移动平均（EWMA），新样本权重 `α=0.3`。

```
speed_t = 0.3 * sample + 0.7 * speed_{t-1}
```

**失效与黑名单**：

| 触发条件 | 动作 | 持续 |
|---------|------|------|
| 5xx 连续 3 次 | 该 (executor, source) 标 degraded | 5 min（指数退避到 30 min） |
| SHA256 不匹配 | 该 (source, repo, filename) 黑名单 | 24h |
| `health_check` 超时 ≥ 30s | 该 source 全局降级，回退到 HF | 直到下次成功探测 |
| 用户主动拉黑 | 写入 `task.source_blacklist` | 任务级别永久 |

### 1.8 一键多源（用户视角）

**UI / API 行为**：

任务创建时选择 `source_strategy`：

```
[ ] 仅 HuggingFace
[x] 自动多源加速  ← 默认
[ ] 自定义：[ ✓ HF  ✓ Mirror  ✓ ModelScope  ☐ WiseModel ]
[ ] 仅自托管 mirror（内网模式）

进阶：
[x] 启动前实时测速（推荐，约 5-15 秒）
[ ] 仅用历史画像（启动快但首跑可能不优）
```

**自动多源加速** 的内部行为，分两阶段：

#### 阶段 A：启动前实时测速 + 最优组合选择（task=scheduling 时）

```python
async def select_optimal_source_combination(
    task: DownloadTask,
    candidate_sources: list[SourceDriver],
    eligible_executors: list[Executor],
) -> SourceCombination:
    """
    任务启动前 5-15 秒快速测速，选最优源子集。
    返回：每个 (executor, source) 的速度估计 + 最终启用的源集合。
    """
    # Step 1: 每个 executor 对每个候选源做并发测速
    probe_size_mb = config.probe_size_mb       # 默认 32MB
    probe_timeout_s = config.probe_timeout_s   # 默认 8s
    deadline = now() + probe_timeout_s

    # 全 N×M 并发探测，N=executor 数，M=源数
    matrix: dict[tuple[str, str], float] = {}  # (executor_id, source_id) -> bytes/sec
    async with asyncio.TaskGroup() as tg:
        for exec_ in eligible_executors:
            for src in candidate_sources:
                tg.create_task(
                    _probe_one(exec_, src, task.repo_id, probe_size_mb, deadline, matrix)
                )

    # Step 2: 与历史 EWMA 融合（实测权重 0.7，历史 0.3）
    for key in matrix:
        hist = await load_ewma(key)
        matrix[key] = 0.7 * matrix[key] + 0.3 * hist if hist else matrix[key]

    # Step 3: 选最优源子集（不一定全用，少用慢源能减少协调开销）
    return _solve_optimal_combination(matrix, task.files, candidate_sources)


async def _probe_one(executor, source, repo_id, probe_mb, deadline, matrix):
    """
    用任务自身的小文件做测速（更准），fallback 用源提供的标准探测路径。
    """
    try:
        # 优选：从将下载的 manifest 里挑一个 ~probe_mb 大小的真实文件
        probe_file = pick_probe_file(repo_id, target_size_mb=probe_mb)
        url_or_key = source.download_endpoint_for(probe_file)

        start = monotonic()
        bytes_recv = 0
        async with executor.proxy_get(url_or_key, range=(0, probe_mb*1024*1024)) as resp:
            async for chunk in resp:
                bytes_recv += len(chunk)
                if monotonic() > deadline:
                    break  # 软超时，已收的字节作为样本
        elapsed = monotonic() - start
        matrix[(executor.id, source.id)] = bytes_recv / elapsed if elapsed > 0 else 0.0
    except Exception as e:
        # 探测失败 → 速度记 0，效果等同此源在此 executor 不可用
        matrix[(executor.id, source.id)] = 0.0
        log.warn(f"probe failed: exec={executor.id} src={source.id} err={e}")
```

**最优组合选择算法**（`_solve_optimal_combination`）：

不是简单"全用"。引入更慢的源会摊薄文件分配，可能反而拖慢整体。判定如下：

```python
def _solve_optimal_combination(matrix, files, sources) -> SourceCombination:
    # 候选组合：所有非空子集，但优先评估"最快的 K 个源"
    sources_by_max_speed = sorted(
        sources,
        key=lambda s: -max(matrix.get((e, s.id), 0) for e in eligible_executors)
    )

    best_eta = float("inf")
    best_combo = None
    for k in range(1, len(sources_by_max_speed) + 1):
        combo = sources_by_max_speed[:k]
        # 模拟 LPT 分配，估算总耗时
        eta = simulate_lpt_eta(matrix, files, combo)
        # 加入"协调开销惩罚"：每多一个源 +2% 开销
        eta_with_overhead = eta * (1 + 0.02 * (k - 1))
        if eta_with_overhead < best_eta:
            best_eta = eta_with_overhead
            best_combo = combo
        else:
            # 单调递增意味着加更多源没有边际收益
            if k > 1 and eta_with_overhead > best_eta * 1.05:
                break
    return SourceCombination(combo=best_combo, expected_eta=best_eta, matrix=matrix)
```

**展示给用户的测速结果**（UI 在任务创建后 ~10 秒内显示）：

```
任务 deepseek-ai/DeepSeek-V3 启动测速中... (8.2s)

测速结果（每秒字节，越大越好）：
                    node-1     node-2     node-3     node-4
  ModelScope         950 MB/s   920 MB/s   880 MB/s   910 MB/s   ← 推荐
  HF Mirror          420 MB/s   410 MB/s   430 MB/s   400 MB/s   ← 推荐
  HuggingFace         85 MB/s    78 MB/s    92 MB/s    81 MB/s   ← 跳过（太慢）
  WiseModel          240 MB/s   235 MB/s   210 MB/s   220 MB/s   ← 跳过（边际收益低）

最优组合：ModelScope + HF Mirror
预计总耗时：18 分钟（vs 单 HuggingFace ~3.5 小时）
开始下载...
```

#### 阶段 B：下载中持续校准（task=downloading 时）

启动后每 60 秒：

1. 把已完成 chunk 的实际速度（`bytes_done / duration`）写回 `source_speed_samples` + EWMA
2. 检测异常退化：某 (executor, source) 速度跌至 启动测速的 30% 以下 → 触发"局部重平衡"
3. 局部重平衡：把该源未开始的剩余 chunk 重分给其他源（已开始的不打断）
4. 新源恢复：原本被跳过的源若现在测速更新且超过启用源的 50%，加入候选并重分剩余 chunk

```sql
-- 局部重平衡的原子操作（SKIP LOCKED 防止与下载竞争）
UPDATE subtask_chunks
SET source_id = $new_source, status = 'pending'
WHERE id IN (
  SELECT id FROM subtask_chunks
  WHERE source_id = $degraded_source AND status = 'pending'
  FOR UPDATE SKIP LOCKED
);
```

#### 阶段 C：测速参数与失败处理

| 参数 | 默认 | 调优建议 |
|------|------|---------|
| `probe_size_mb` | 32 | 大模型可加大到 64；小模型小到 8 |
| `probe_timeout_s` | 8 | 网络差时给到 15 |
| `probe_freshness_history_weight` | 0.3 | 历史画像权重 |
| `combo_overhead_per_source_pct` | 2 | 每多一个源的协调开销假设 |
| `degradation_trigger_threshold` | 0.3 | 跌至启动测速 30% 触发局部重平衡 |
| `recovery_consider_threshold` | 0.5 | 恢复源速度需达到现用源 50% 才考虑加入 |

**测速失败的兜底**：

- 全部源测速 = 0：任务进入 `paused_external`，5min 后重试
- 仅 1 个源测速成功：直接单源下，无加速
- HF 测速失败但其他源成功 + 用户未启用 `--trust-non-hf-sha256` → 任务暂停（不变量 13）

**测速成本估算**（默认配置下）：

- 32MB × 5 sources × 4 executors = 640MB 测速流量
- 对 689GB 任务而言，开销 = 0.09%，可忽略
- 优势：避免错误地把 60% 流量分给一个跨境慢源

**降级矩阵**（用户配置 `auto_balance` 时）：

| 状态 | 行为 |
|------|------|
| 至少 2 个健康源 | LPT file-level + 大文件 chunk-level |
| 仅 1 个健康源 | 单源全速下，行为退化为 v1.x |
| 全部源失效 | 任务进入 `paused_external`，5min 后重试探测 |
| HF 不可用但其他源可用 | ⚠️ 暂停下载（因 SHA256 来源缺失），除非用户显式开启 `--trust-non-hf-sha256` |

🔒 **不变量 13：HF 不可用时，默认拒绝下载**（除非用户显式信任非 HF 源 sha256，需在合规章节 04 §5 走审批）。

### 1.9 内置源驱动实现要点

#### 1.9.1 HuggingFace 驱动

- 使用 `huggingface_hub` SDK 的 `HfApi`
- `resolve()` 调 `/api/models/{repo_id}/revision/{rev}` + `/api/models/{repo_id}/tree/{rev}?recursive=1`
- `download_range()` 走 `huggingface.co/{repo}/resolve/{rev}/{filename}` 带 Range header
- ⚠️ HF CDN URL 有时限：每次 request 都重新拿，不缓存（避免 N-02）
- Token 由 controller 注入，executor 走 controller proxy

#### 1.9.2 HF Mirror（hf-mirror.com）驱动

- 与 HF 完全兼容协议，仅替换 base URL = `https://hf-mirror.com`
- ⚠️ 不带 Token（公开镜像不支持 gated 模型）
- gated 模型自动跳过此源

#### 1.9.3 ModelScope 驱动

- 使用 `modelscope` SDK 或 raw HTTP
- API: `https://www.modelscope.cn/api/v1/models/{repo_id}/repo?Revision={rev}`
- 文件下载：`https://www.modelscope.cn/api/v1/models/{repo_id}/repo?Revision={rev}&FilePath={filename}`
- ⚠️ 不提供官方 sha256；依赖 HF 的 sha256 校验
- 名字映射：见 §1.5

#### 1.9.4 WiseModel 驱动

- API 类 HF：`https://www.wisemodel.cn/api/v1/{repo}/resolve/{rev}/{filename}`
- ⚠️ 覆盖率有限，仅作为 ModelScope 不可达时的备份
- ⚠️ 不提供 sha256

#### 1.9.5 OpenCSG 驱动

- API：`https://opencsg.com/api/v1/models/{repo}/resolve/{rev}/{filename}`
- 与 WiseModel 类似定位

#### 1.9.6 S3 Mirror（自托管）驱动

- 用户配置 endpoint + bucket + 路径模板
- 路径模板：`s3://{bucket}/{prefix}/{repo_id}/{revision}/{filename}`
- 凭证：用户提供长期 AK/SK 或 IRSA / instance profile
- ⚠️ 用户自己保证内容与 HF 一致；启动时控制器抽样校验 3 个文件
- 优势：内网走，速度可达千兆，0 流量成本

### 1.10 第三方源插件机制

```python
# plugins/myorg_internal_source.py
from dlw.sources import SourceDriver, register_source

class MyOrgInternalDriver(SourceDriver):
    id = "myorg_internal"
    domain = "models.myorg.internal"
    # ...

register_source(MyOrgInternalDriver())
```

控制器启动时扫描 `plugins/` 目录加载第三方驱动。

### 1.11 一键多源的可视化

UI 任务详情页新增"源分配视图"：

```
┌──────────────────────────────────────────────────────────────────────┐
│ 任务 deepseek-ai/DeepSeek-V3 @ abc123  共 689 GB / 163 文件             │
├──────────────────────────────────────────────────────────────────────┤
│ 源分配 (file-level)                                                    │
│   ModelScope     ████████████████████████████████   62%  428 GB         │
│   HF Mirror      ████████████                       28%  193 GB         │
│   HuggingFace    ████                                10%   68 GB        │
│                                                                         │
│ 实测速度（EWMA）                                                        │
│   ModelScope     950 MB/s  ← node-1, node-2                             │
│   HF Mirror      420 MB/s  ← node-3                                     │
│   HuggingFace     85 MB/s  ← node-4 (跨境)                              │
│                                                                         │
│ 大文件 chunk-level（model-00001-of-00163.safetensors, 4.3GB）            │
│   ┌─ chunk 0/8 (537 MB) ── ModelScope ── ✅                              │
│   ├─ chunk 1/8 (537 MB) ── HF Mirror   ── ✅                              │
│   └─ chunk 2/8 (537 MB) ── ModelScope ── 🔄 进行中 412/537 MB             │
│                                                                         │
│ 健康                                                                    │
│   HuggingFace  🟢  HF Mirror  🟢  ModelScope  🟢  WiseModel  ⚫ disabled   │
└──────────────────────────────────────────────────────────────────────┘
```

### 1.12 sources.yaml 完整配置示例

```yaml
sources:
  - id: huggingface
    enabled: true
    driver: huggingface
    config:
      base_url: https://huggingface.co
      timeout_seconds: 30
      max_concurrent_per_executor: 8
    cost_per_gb_egress: 0.09  # USD/GB（HF 无显式费用，但内网出站费用）

  - id: hf_mirror
    enabled: true
    driver: hf_mirror
    config:
      base_url: https://hf-mirror.com
      timeout_seconds: 30
    cost_per_gb_egress: 0.0

  - id: modelscope
    enabled: true
    driver: modelscope
    config:
      base_url: https://www.modelscope.cn
      timeout_seconds: 30
      ms_token_secret_ref: vault://secrets/modelscope_token  # 可选
    cost_per_gb_egress: 0.0

  - id: wisemodel
    enabled: false
    driver: wisemodel

  - id: opencsg
    enabled: false
    driver: opencsg

  - id: corp_mirror
    enabled: true
    driver: s3_mirror
    config:
      endpoint: https://oss.myorg.internal
      bucket: model-mirror
      prefix: huggingface
      region: cn-north-1
      auth_secret_ref: vault://secrets/corp_mirror_aksk
    cost_per_gb_egress: 0.0
    priority_boost: 10  # 内网优先

balancing:
  strategy: lpt                  # lpt / round_robin / fastest_only
  speed_ewma_alpha: 0.3
  speed_probe_interval_seconds: 300
  chunk_level_min_file_mb: 100
  speed_window_minutes: 30
  blacklist_after_5xx_count: 3
  blacklist_duration_minutes: 5
  blacklist_max_minutes: 30      # 指数退避上限
  sha_mismatch_blacklist_hours: 24

regional_defaults:
  cn-north: [hf_mirror, modelscope, huggingface]
  ap-southeast: [huggingface, modelscope, hf_mirror]
  us-east: [huggingface]
```

### 1.13 实施风险与已知问题

⚠️ **已知风险 1：ModelScope/WiseModel 不提供官方 SHA256**
依赖 HF 的 SHA256 做最终校验。如果 HF 上对应模型没有 LFS（即 sha256 缺失，常见于早期/小模型），无法做内容校验，需在 UI 显眼提示用户"该模型不能多源加速"。

⚠️ **已知风险 2：HF force-push 期间跨源 commit 不一致**
HF 上 `revision=main` 在我们下载时可能被 push。即便 user 提供 `revision=&lt;sha&gt;`，其他源的同名 revision 可能滞后几小时。解决：
- 控制器在 `resolve` 阶段验证 HF 与其他源对此 revision 的存在性
- 校验失败的源标记为 "stale"，跳过

⚠️ **已知风险 3：法务 / 出口管制**
有些模型在 HF 上是 gated（需协议），但其他源可能无视协议（合法性灰色）。详见 04 §8 合规章节，多源功能默认尊重 HF gated 状态。

---

## 2. 增量 / 差分下载

### 2.1 动机

模型小版本升级（如 tokenizer 改一个文件、PEFT 增量权重）目前要重下整个 689GB。基于文件级 SHA256 与已存 revision 比较，可只下变化的文件。

### 2.2 设计

任务创建时支持 `upgrade_from_revision` 字段：

```http
POST /api/tasks
{
  "repo_id": "deepseek-ai/DeepSeek-V3",
  "revision": "new-sha-456",
  "upgrade_from_revision": "old-sha-123",   # 已下载完成的旧 revision
  ...
}
```

控制器逻辑：

1. `resolve(repo_id, new_rev)` 得到新文件列表与 sha256
2. 查 DB 中 `(tenant_id, repo_id, old_rev)` 的所有 subtasks，含其 `actual_sha256`
3. **diff**：
   - sha 相同的文件：在新任务中标记 `inherit_from=old_subtask_id`，状态直接 `verified`
   - sha 不同 / 新增的文件：正常下载
   - 旧版本独有的文件：跳过（不删旧 revision，因可能其他任务在引用）
4. 存储侧用硬链接 / S3 server-side copy 避免重传：
   - 本地存储：`os.link(old_path, new_path)`
   - S3：`copy_object(CopySource=old_key, Bucket, Key=new_key)`（同 region 几乎免费）

### 2.3 节省估计

实测：Qwen3-72B 从 v1.0 → v1.1，仅 tokenizer.json + 5 个 .safetensors 改变 ≈ 8GB，省 94%。

### 2.4 与多源的关系

增量下载 + 多源可叠加：从 sha 表先消除已存文件，剩余文件再走 §1 的多源 LPT 分配。

---

## 3. 模型资产管理

### 3.1 全局去重

🔒 **不变量 14：同一 (tenant_id, repo_id, revision, filename, sha256) 在存储中只存一份**

实现：`storage_objects` 表

```sql
CREATE TABLE storage_objects (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       BIGINT NOT NULL,
    storage_id      BIGINT NOT NULL,
    storage_key     VARCHAR(1024) NOT NULL,
    sha256          VARCHAR(64) NOT NULL,
    size            BIGINT NOT NULL,
    refcount        INT NOT NULL DEFAULT 1,
    last_referenced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, storage_id, sha256)
);

CREATE TABLE subtask_object_refs (
    subtask_id    UUID NOT NULL REFERENCES file_subtasks(id),
    object_id     BIGINT NOT NULL REFERENCES storage_objects(id),
    PRIMARY KEY (subtask_id, object_id)
);
```

任务完成时：

- subtask 上传完成 → 查同 tenant 是否已有同 sha256 → 有则 abort 上传 + refcount++（实际节省）
- subtask 删除时 refcount--；为 0 时由 GC 异步删

### 3.2 引用计数与 LRU 淘汰

**淘汰策略**：

- 配额触发：tenant 存储用量 ≥ 90% 配额，按 `last_referenced_at` LRU 淘汰 refcount=0 的对象
- 时间触发：refcount=0 + 超过 `archive_after_days`（默认 90 天）→ 移到冷存储或删除（按租户策略）

### 3.3 删除前依赖检查

```http
DELETE /api/tasks/{id}
```

- 仅删 task 与 subtask 记录
- subtask_object_refs 删除导致 refcount 减
- refcount=0 的 storage_objects 进入"待清理"队列，每日 GC 扫描

### 3.4 定时探查与自动下载

继承 v1.4 §10 设计，但加多租户：

```yaml
# 每个 tenant 可配置自动探查规则
auto_probe_rules:
  - name: "Qwen 系列新版本"
    repo_pattern: "Qwen/Qwen*"
    schedule: "0 */6 * * *"           # 每 6 小时
    auto_create_task: true
    storage_id: 5
    notify_users: [admin@team.com]

  - name: "DeepSeek 全收"
    repo_pattern: "deepseek-ai/*"
    schedule: "0 0 * * *"             # 每日
    auto_create_task: false           # 仅通知，人工确认
```

### 3.5 模型完整文件清单策略

继承 v1.4 §11，明确：

- **白名单模式**（默认）：仅下载 `model-*.safetensors`、`config.json`、`tokenizer*` 等核心文件
- **完整模式**：下 repo 下所有文件
- **用户自定义**：glob 模式

⚠️ pickle 文件 (`*.bin`, `*.pickle`, `*.pt`) 默认拒绝（详见 04 §5）。

---

## 4. 平台集成

### 4.1 Webhook

任务事件触发 webhook：

```yaml
# 租户级配置
webhooks:
  - url: https://team.slack.com/services/...
    events: [task.completed, task.failed]
    secret_ref: vault://secrets/slack_webhook
  - url: https://internal-mlops/dlw-webhook
    events: ["*"]
```

事件 payload：

```json
{
  "event": "task.completed",
  "occurred_at": "2026-04-28T10:30:00Z",
  "tenant_id": 1,
  "task": {
    "id": "uuid",
    "repo_id": "deepseek-ai/DeepSeek-V3",
    "revision": "abc123",
    "total_bytes": 740088332288,
    "duration_seconds": 1820,
    "storage_uri": "s3://bucket/path/...",
    "sources_used": ["modelscope", "hf_mirror"]
  },
  "signature": "sha256=..."   // HMAC for verification
}
```

### 4.2 MLflow Model Registry 自动注册

```yaml
integrations:
  mlflow:
    enabled: true
    tracking_uri: https://mlflow.myorg/
    auto_register_on_complete: true
    register_as: "{tenant}/{repo_id}"
```

任务完成 → 自动调 `mlflow.register_model(uri=...)` 并 tag。

### 4.3 K8s Operator + ModelDownload CRD

```yaml
apiVersion: dlw.io/v1
kind: ModelDownload
metadata:
  name: qwen3-72b
  namespace: ml-team
spec:
  repoId: Qwen/Qwen3-72B-Instruct
  revision: abc123
  storageBackendRef: my-s3
  sourceStrategy: auto_balance
  notifyOnComplete:
    slackWebhook: ...
status:
  phase: Downloading
  progress: 67
  controllerTaskId: uuid-...
```

Operator 监听 CRD → 调 controller API 创建任务 → poll status → update CRD status。

### 4.4 HF cache 兼容（HF_HOME 透明代理）

让用户在 transformers / vLLM 中无缝走本系统：

```bash
export HF_HOME=/mnt/dlw-cache
export HF_HUB_OFFLINE=1
```

`/mnt/dlw-cache` 是控制器导出的 FUSE mount 或 NFS：

- 目录结构遵循 HF cache 约定：`hub/models--{org}--{model}/snapshots/{revision}/`
- 用户代码 `from_pretrained(...)` 命中本地缓存，缺失时通过 mount 触发懒下载（调控制器 API）
- 已下载完成的模型立即可用，不存在的模型自动建任务

### 4.5 vLLM / SGLang / transformers 直接消费

- 任务 metadata 暴露 `storage_uri` + `path_template`，用户脚本直接 `from_pretrained(path)`
- 提供 `dlw materialize <task_id> --to-local /path`：从存储拉到本地工作目录

---

## 5. CLI / SDK / 用户接入层

### 5.1 CLI（`dlw`）

```bash
# 认证（OIDC device code flow）
dlw login

# 提交任务（自动多源加速）
dlw submit deepseek-ai/DeepSeek-V3 --revision abc123 --priority 2

# 列出我的任务
dlw list --status downloading

# 查看任务详情（含源分配）
dlw show <task-id>

# 跟踪进度（attach 模式）
dlw watch <task-id>

# 取消任务
dlw cancel <task-id>

# 增量更新
dlw upgrade <task-id> --to-revision new-sha

# 批量提交（任务模板）
dlw submit-batch ./templates/qwen-family.yaml

# 同步到本地工作目录
dlw materialize <task-id> --to ./models/qwen3-72b

# 配额查看
dlw quota
```

### 5.2 Python SDK

```python
from dlw import Client

client = Client.from_env()           # 读 ~/.dlw/credentials

# 同步等
task = client.submit(
    repo_id="deepseek-ai/DeepSeek-V3",
    revision="abc123",
    source_strategy="auto_balance",
)
task.wait(timeout=3600, on_progress=lambda p: print(f"{p:.0%}"))

# 异步
async with client.async_session() as s:
    task = await s.submit(...)
    async for evt in task.stream_events():
        print(evt)
```

### 5.3 任务模板 / 收藏

```yaml
# templates/qwen-family.yaml
name: "Qwen3 family weekly snapshot"
storage: project-default
priority: 1
source_strategy: auto_balance
file_filter: core_only            # 只下核心文件
tasks:
  - repo_id: Qwen/Qwen3-72B-Instruct
    revision: latest_sha
  - repo_id: Qwen/Qwen3-32B-Instruct
    revision: latest_sha
  - repo_id: Qwen/Qwen3-7B-Instruct
    revision: latest_sha
```

### 5.4 文件级 Resume（用户视角）

CLI 中断后再启动：自动续传未完成的 file，已 sha 通过的 file 跳过。

---

## 6. 单机多执行器 UX

继承 v1.5 §3，关键点：

- 用户配置多 executor 时不需要装多次 docker；用 `--executors-per-host=4` 启动单容器多进程
- UI "Hosts" 视图按 host_id 聚合，每个 host 显示 NIC 利用率与 host 下 executor 列表
- 调度器 multi-executor-aware（不变量 10）

---

## 7. UI 详细设计

> 取代：v1.4 §6 全章。重复绘制（v1.4 §6.4 / §6.8 / §12.5）已合并。

### 7.1 视图列表

| 视图 | 路径 | 角色 |
|------|------|------|
| 概览 Dashboard | `/` | 任意 |
| 任务列表 | `/tasks` | 任意 |
| 任务详情 | `/tasks/{id}` | 任意（仅看自己 tenant） |
| 节点列表 | `/executors` | operator+ |
| 节点详情 | `/executors/{id}` | operator+ |
| 模型搜索 | `/search` | 任意 |
| 模板管理 | `/templates` | 任意 |
| 配额与计量 | `/quota` | admin |
| 审计日志 | `/audit` | admin（且 RBAC: audit.read） |
| 系统设置 | `/settings` | admin |

### 7.2 任务详情核心组件

1. **基本信息**：repo / revision / 状态 / 源策略 / 优先级 / owner
2. **进度面板**：总进度环 + 速度 + ETA
3. **源分配视图**（新增，详见 §1.11）
4. **文件矩阵**：163 个文件的 4 列网格，颜色编码状态
5. **节点参与**：哪些 executor 参与，各自吞吐
6. **事件日志**：状态变更、错误、重试
7. **操作**：取消 / 重试失败子任务 / 调优先级

### 7.3 实时更新协议

详见 02 §5：snapshot+delta+seq 模式。UI 重连时主动拉一次 snapshot。

### 7.4 安全

- `v-html` 全局禁用（lint 检查）
- 来自 executor 的字符串（filename, error_message）必须经 escape
- CSP: `default-src 'self'; script-src 'self'`
- WS Origin 白名单 + JWT 子协议（详见 04 §3）

---

## 8. E2E 测试

继承 v1.5 §5，多源相关补充：

```python
# tests/e2e/test_multi_source.py

def test_auto_balance_uses_fastest_source(mock_sources):
    # 配置：modelscope 1GB/s, hf_mirror 500MB/s, hf 50MB/s
    task = client.submit("test/repo", source_strategy="auto_balance")
    task.wait()
    assert task.sources_used == {"modelscope", "hf_mirror"}  # hf 太慢被跳过

def test_source_failover_on_5xx(mock_sources_with_intermittent_5xx):
    task = client.submit("test/repo", source_strategy="auto_balance")
    task.wait()
    assert task.status == "completed"
    assert any(c.retried_on_other_source for c in task.chunks)

def test_sha256_mismatch_blacklists_source(corrupted_modelscope):
    task = client.submit("test/repo", source_strategy="auto_balance")
    task.wait()
    assert task.status == "completed"
    blacklist = client.get_source_blacklist()
    assert ("modelscope", "test/repo", "model.safetensors") in blacklist

def test_hf_unavailable_pauses_task_by_default():
    # HF down，其他源可用 → 默认拒绝下载（不变量 13）
    task = client.submit("test/repo")
    assert task.status == "paused_external"
    assert "no_sha256_authority" in task.last_error

def test_user_explicit_trust_non_hf_sha256():
    task = client.submit("test/repo", trust_non_hf_sha256=True)
    # 需要 admin 审批，工作流测试
    ...

def test_chunk_level_routing_for_large_file():
    # 4.3GB 单文件，2 个源都覆盖
    task = client.submit("test/big-model")
    task.wait()
    assert task.has_chunk_level_routing
    assert task.chunks_by_source == {"modelscope": 5, "hf_mirror": 3}
```

---

## 9. Roadmap（v2.1+）

| 主题 | 计划版本 | 备注 |
|------|---------|------|
| 跨地域复制（auto-replicate） | v2.1 | 北京下完自动 sync 到香港 |
| 控制器 active-active | v2.2 | 当前 v2.0 仅 active/standby |
| SLA 分级（class-of-service） | v2.1 | critical / standard / best-effort，含抢占 |
| 行为遥测 + 热门模型预热 | v2.1 | 系统自动预下热门模型 |
| 离线 / 气隙 export bundle | v2.1 | 外网下→打包→内网 import |
| Sigstore 验签 | v2.2 | 与 HF 上游协同 |
| 模型量化在线转换 | v2.2 | 下载完直接生成 GGUF/AWQ |
| 多源 chunk-level + 流式哈希 | v2.2 | 需要 BLAKE3 全面采用 |

---

## 10. 与其他文档的链接

- 源协议安全：→ [04-security-and-tenancy.md](./04-security-and-tenancy.md) §3 凭证、§5 供应链
- 源调度的 fence/恢复语义：→ [03-distributed-correctness.md](./03-distributed-correctness.md) §4 调度
- 源 metrics / SLO：→ [05-operations.md](./05-operations.md) §1 三柱
- API：→ [02-protocol.md](./02-protocol.md) §3 任务 API（含 source_strategy 字段）
- 数据模型：→ [01-architecture.md](./01-architecture.md) §4
