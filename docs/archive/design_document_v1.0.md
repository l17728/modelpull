# [SUPERSEDED] 分布式 HuggingFace 模型权重下载系统 — 设计文档 v1.0

> ⚠️ **此文档已被 v2.0 取代，仅作历史追溯**
>
> 当前权威文档：**[../v2.0/00-INDEX.md](../v2.0/00-INDEX.md)**
>
> v2.0 修复了 v1.0 中的关键问题，包括：状态机三处定义不一致、心跳/任务模型字段漂移、
> Executor 注册零认证、HF Token 明文下发、SHA256 真值断裂、Runbook/SLO 完全缺失、
> 多租户/配额/合规未支持等。详见 v2.0 的 00-INDEX.md "v2.0 相对历史版本的变化" 章节。
>
> **请勿基于本文档实施。** 实施时以 v2.0 为准。

> 版本: v1.0（已废弃）| 原日期: 2026-04-28

---

## 目录

1. [项目背景与目标](#1-项目背景与目标)
2. [HuggingFace API 调研报告](#2-huggingface-api-调研报告)
3. [需求分析与断点续传可行性](#3-需求分析与断点续传可行性)
4. [系统架构设计](#4-系统架构设计)
5. [控制器 (Controller) 详细设计](#5-控制器-controller-详细设计)
6. [下载执行器 (Executor) 详细设计](#6-下载执行器-executor-详细设计)
7. [Web UI 详细设计](#7-web-ui-详细设计)
8. [任务调度与负载均衡](#8-任务调度与负载均衡)
9. [数据传输与组装](#9-数据传输与组装)
10. [完整性与一致性校验](#10-完整性与一致性校验)
11. [进度监控与 ETA 估算](#11-进度监控与-eta-估算)
12. [技术选型](#12-技术选型)
13. [API 接口定义](#13-api-接口定义)
14. [部署方案](#14-部署方案)
15. [项目结构](#15-项目结构)

---

## 1. 项目背景与目标

### 1.1 背景

当前大语言模型（如 DeepSeek-V3/V4、Kimi-K2、GLM-4 等）的权重文件规模巨大：

| 模型 | 参数量 | 总大小 | 分片数 | 单分片大小 |
|------|--------|--------|--------|-----------|
| DeepSeek-V3 (FP8) | 671B | 689 GB | 163 | ~4.3 GB |
| Kimi-K2-Instruct (FP8) | ~1T | 1.03 TB | 61 | ~17.1 GB |
| GLM-4-9b-Chat (BF16) | 18.6B | 18.5 GB | 10 | ~1.9 GB |

单机下载 TB 级模型耗时极长，需要分布式下载方案来提升效率。

### 1.2 目标

- 提供可视化 UI，支持搜索 HuggingFace 模型、创建下载任务、监控进度
- 分布式执行器架构，支持多台机器并行下载
- 每个执行器内部多线程分块下载，充分利用带宽
- 支持断点续传、动态扩缩容、负载均衡
- 下载完成后自动组装、校验完整性

---

## 2. HuggingFace API 调研报告

### 2.1 模型搜索 API

```
GET https://huggingface.co/api/models
```

**关键参数：**

| 参数 | 说明 | 示例 |
|------|------|------|
| `search` | 模型 ID 子串搜索 | `deepseek` |
| `author` | 按作者/组织筛选 | `deepseek-ai` |
| `filter` | 按标签筛选（库/任务/语言） | `pytorch`, `text-generation` |
| `pipeline_tag` | 按任务类型筛选 | `text-generation` |
| `sort` | 排序字段 | `downloads`, `likes`, `last_modified` |
| `limit` | 返回数量 | `50` |
| `full` | 返回完整元数据 | `true` |
| `cardData` | 包含模型卡片数据 | `true` |
| `num_parameters` | 参数量范围 | `min:6B,max:128B` |
| `gated` | 筛选受限模型 | `true` |

**Python 调用：**
```python
from huggingface_hub import HfApi
api = HfApi()

models = api.list_models(
    search="deepseek",
    sort="downloads",
    limit=20,
    cardData=True
)
for m in models:
    print(m.id, m.downloads, m.pipeline_tag)
```

### 2.2 模型详情 API

```
GET https://huggingface.co/api/models/{repo_id}
GET https://huggingface.co/api/models/{repo_id}/revision/{revision}
```

**参数 `files_metadata=True` 可返回文件大小和 LFS 信息。**

```python
info = api.model_info("deepseek-ai/DeepSeek-V3", files_metadata=True)

for sibling in info.siblings:
    print(sibling.rfilename, sibling.size)
    if sibling.lfs:
        print(f"  SHA256: {sibling.lfs.sha256}")
        print(f"  Size:   {sibling.lfs.size}")
```

### 2.3 文件树 API

```
GET https://huggingface.co/api/models/{repo_id}/tree/{revision}/{path}
```

```python
files = api.list_repo_tree("deepseek-ai/DeepSeek-V3", recursive=True)
for f in files:
    if isinstance(f, RepoFile):
        print(f.path, f.size, f.lfs.sha256 if f.lfs else None)
```

### 2.4 文件下载

**直接下载 URL：**
```
https://huggingface.co/{repo_id}/resolve/{revision}/{filename}
```

**响应头：**
| Header | 说明 |
|--------|------|
| `X-Repo-Commit` | Git commit hash |
| `X-Linked-Etag` | 文件哈希（Git 文件=git-sha1, LFS 文件=SHA256） |
| `X-Linked-Size` | 实际文件大小 |
| `Location` | CDN 重定向地址 |

**Range 请求完全支持：**
```
GET /deepseek-ai/DeepSeek-V3/resolve/main/model-00001-of-00163.safetensors
Range: bytes=1048576-
→ 206 Partial Content
Content-Range: bytes 1048576-4592318463/4592318464
```

### 2.5 文件完整性校验

- **LFS 文件**：通过 `model_info(files_metadata=True)` 获取 `sibling.lfs.sha256`
- **普通文件**：通过 `X-Linked-Etag` 响应头获取 git-sha1
- **分片索引**：`model.safetensors.index.json` 中包含 `metadata.total_size`

### 2.6 认证

```python
# 方式1: 登录
huggingface-cli login

# 方式2: 环境变量
export HF_TOKEN=hf_xxxxxxxx

# 方式3: 代码中传入
api = HfApi(token="hf_xxxxxxxx")
```

公共模型无需 Token；私有/受限模型需要 Token。

---

## 3. 需求分析与断点续传可行性

### 3.1 断点续传分析

**结论：必须支持断点续传。**

理由：
1. 单个分片文件可达 17GB（Kimi-K2），下载耗时长，网络中断概率高
2. HuggingFace CDN 完全支持 HTTP Range 请求
3. 每个文件都有 SHA256 校验值可用于验证

**实现方案：**
- 下载时将已下载部分保存为 `.incomplete` 文件
- 记录已下载字节数到状态文件（JSON）
- 恢复时发送 `Range: bytes={已下载字节数}-` 请求剩余部分
- 下载完成后用 SHA256 校验完整性

### 3.2 任务分片策略

**分片粒度：以文件为单位。**

理由：
1. 模型权重天然由多个独立的 safetensors 分片文件组成（如 DeepSeek-V3 有 163 个分片）
2. 每个分片文件是独立的二进制文件，无需关心文件内部逻辑
3. 配置文件（config.json, tokenizer 等）体积小，可单独分配或由控制器直接下载
4. 以文件为单位便于断点续传和校验

**额外优化 — 文件内多线程分块：**
对于大文件，单个执行器内部使用多线程按字节范围并行下载：
```
文件: model-00001-of-00163.safetensors (4.3 GB)
线程1: bytes=0-1073741823          (0-1GB)
线程2: bytes=1073741824-2147483647  (1-2GB)
线程3: bytes=2147483648-3221225471  (2-3GB)
线程4: bytes=3221225472-4592318463  (3-4.3GB)
```
每个线程下载一个 chunk，写入临时分块文件，全部完成后拼接为完整文件。

### 3.3 动态扩容

新增执行器注册后，控制器需要重新平衡：
- 从已有执行器中**回收未开始**的文件任务，重新分配
- 已在下载中的任务**不打断**，仅迁移待下载任务
- 如果所有任务均已分配，新执行器进入待机状态

---

## 4. 系统架构设计

### 4.1 总体架构

```
┌──────────────────────────────────────────────────────────────┐
│                        Web UI (前端)                          │
│  React/Vue3 + WebSocket 实时进度 + HuggingFace 搜索           │
└───────────────────────────┬──────────────────────────────────┘
                            │ HTTP REST + WebSocket
┌───────────────────────────┴──────────────────────────────────┐
│                   Controller (控制器)                          │
│                                                               │
│  ┌─────────┐  ┌──────────┐  ┌─────────┐  ┌───────────────┐  │
│  │ HF API  │  │  Task    │  │ Executor│  │ Data Assembly │  │
│  │ Search  │  │ Scheduler│  │ Manager │  │ & Verification│  │
│  └─────────┘  └──────────┘  └─────────┘  └───────────────┘  │
│  ┌─────────┐  ┌──────────┐  ┌─────────┐  ┌───────────────┐  │
│  │ Progress│  │ Heartbeat│  │ Balance │  │   Storage     │  │
│  │ Monitor │  │ Checker  │  │ Adjuster│  │   Manager     │  │
│  └─────────┘  └──────────┘  └─────────┘  └───────────────┘  │
│                                                               │
│  Database: SQLite/PostgreSQL                                 │
└────────┬───────────────┬──────────────┬──────────────────────┘
         │ gRPC/REST     │              │
    ┌────┴────┐    ┌─────┴────┐   ┌─────┴────┐
    │Executor1│    │Executor2 │   │Executor3 │  ... (N 台)
    │         │    │          │   │          │
    │ Download│    │ Download │   │ Download │
    │ Engine  │    │ Engine   │   │ Engine   │
    │         │    │          │   │          │
    │ Chunked │    │ Chunked  │   │ Chunked  │
    │ Multi-  │    │ Multi-   │   │ Multi-   │
    │ thread  │    │ thread   │   │ thread   │
    │         │    │          │   │          │
    │ Assemble│    │ Assemble │   │ Assemble │
    │ & Check │    │ & Check  │   │ & Check  │
    │         │    │          │   │          │
    │ Transfer│    │ Transfer │   │ Transfer │
    │ to Ctrl │    │ to Ctrl  │   │ to Ctrl  │
    └─────────┘    └──────────┘   └──────────┘
```

### 4.2 核心组件

| 组件 | 职责 |
|------|------|
| **Web UI** | 模型搜索、任务创建、进度可视化、执行器管理 |
| **Controller** | 中枢调度：任务管理、执行器管理、进度汇总、数据组装 |
| **Executor** | 下载执行：多线程分块下载、断点续传、完整性校验、传输到控制器 |

### 4.3 通信协议

| 通信方向 | 协议 | 说明 |
|---------|------|------|
| UI ↔ Controller | HTTP REST + WebSocket | REST 用于操作，WebSocket 用于实时进度推送 |
| Executor → Controller | HTTP REST (注册/心跳/上报) | 定期心跳 + 进度上报 |
| Controller → Executor | HTTP REST (任务下发) | 控制器主动推送/执行器轮询任务 |
| Executor → Controller/Storage | HTTP/multipart 或 rsync | 下载完成后传输文件 |

### 4.4 下载完整流程

```
1. 用户在 UI 搜索模型 → Controller 调用 HF API → 返回结果
2. 用户选择模型创建下载任务 → Controller 获取模型文件列表
3. Controller 分析文件列表，创建文件级下载子任务
4. Controller 根据注册执行器数量分配子任务
5. 各 Executor 获取任务 → 多线程分块下载 → 进度上报
6. Executor 完成文件下载 → SHA256 校验 → 传输到 Controller 存储目录
7. Controller 验证接收文件 → 更新任务状态
8. 所有文件完成 → Controller 进行总体验证（文件数、总大小）
9. 任务完成 → UI 显示完成状态
```

---

## 5. 控制器 (Controller) 详细设计

### 5.1 模块划分

#### 5.1.1 HF API 搜索模块

封装 HuggingFace API 调用，为 UI 提供搜索和模型详情接口。

```python
class HFSearchService:
    def __init__(self, token: str = None):
        self.api = HfApi(token=token)

    def search_models(self, query: str, author: str = None,
                      sort: str = "downloads", limit: int = 50) -> list[dict]:
        results = self.api.list_models(
            search=query, author=author, sort=sort,
            limit=limit, cardData=True
        )
        return [self._model_to_dict(m) for m in results]

    def get_model_info(self, repo_id: str, revision: str = "main") -> dict:
        info = self.api.model_info(repo_id, revision=revision, files_metadata=True)
        return {
            "id": info.id,
            "downloads": info.downloads,
            "tags": info.tags,
            "pipeline_tag": info.pipeline_tag,
            "siblings": [
                {
                    "name": s.rfilename,
                    "size": s.size,
                    "sha256": s.lfs.sha256 if s.lfs else None,
                    "is_lfs": s.lfs is not None,
                }
                for s in info.siblings
            ],
            "total_size": sum(s.size or 0 for s in info.siblings),
        }

    def get_model_file_list(self, repo_id: str, revision: str = "main") -> list[dict]:
        info = self.api.model_info(repo_id, revision=revision, files_metadata=True)
        files = []
        for s in info.siblings:
            files.append({
                "name": s.rfilename,
                "size": s.size or 0,
                "sha256": s.lfs.sha256 if s.lfs else None,
                "is_lfs": s.lfs is not None,
            })
        return sorted(files, key=lambda f: f["size"], reverse=True)
```

#### 5.1.2 任务调度模块

```python
class TaskScheduler:
    def create_download_task(self, repo_id: str, revision: str,
                             target_dir: str, token: str = None) -> DownloadTask:
        files = self.hf_service.get_model_file_list(repo_id, revision)
        task = DownloadTask(
            id=uuid4(),
            repo_id=repo_id,
            revision=revision,
            target_dir=target_dir,
            token=token,
            total_files=len(files),
            total_size=sum(f["size"] for f in files),
            status="pending",
            created_at=datetime.utcnow(),
        )
        for f in files:
            subtask = FileSubTask(
                id=uuid4(),
                task_id=task.id,
                filename=f["name"],
                file_size=f["size"],
                expected_sha256=f["sha256"],
                status="pending",
            )
            task.subtasks.append(subtask)
        self.db.save(task)
        self._schedule(task)
        return task

    def _schedule(self, task: DownloadTask):
        pending = [s for s in task.subtasks if s.status == "pending"]
        available = self.executor_manager.get_available_executors()
        if not available:
            return
        for i, subtask in enumerate(pending):
            executor = available[i % len(available)]
            self._assign_subtask(subtask, executor)

    def _assign_subtask(self, subtask: FileSubTask, executor: ExecutorInfo):
        subtask.executor_id = executor.id
        subtask.status = "assigned"
        self.db.update(subtask)
        executor.assign_task(subtask)
```

#### 5.1.3 执行器管理模块

```python
class ExecutorManager:
    HEARTBEAT_TIMEOUT = 30  # 秒

    def __init__(self):
        self.executors: dict[str, ExecutorInfo] = {}
        self._heartbeat_checker = threading.Thread(target=self._check_heartbeats, daemon=True)

    def register(self, executor_id: str, info: ExecutorRegisterRequest) -> ExecutorInfo:
        executor = ExecutorInfo(
            id=executor_id,
            hostname=info.hostname,
            ip=info.ip,
            max_workers=info.max_workers,
            bandwidth_mbps=info.bandwidth_mbps,
            local_storage_path=info.local_storage_path,
            registered_at=datetime.utcnow(),
            last_heartbeat=datetime.utcnow(),
            status="idle",
        )
        self.executors[executor_id] = executor
        self._trigger_rebalance()
        return executor

    def heartbeat(self, executor_id: str, progress: list[TaskProgress]) -> HeartbeatResponse:
        executor = self.executors.get(executor_id)
        if not executor:
            raise ExecutorNotFoundError(executor_id)
        executor.last_heartbeat = datetime.utcnow()
        executor.update_progress(progress)
        new_tasks = self._get_pending_assignments(executor_id)
        return HeartbeatResponse(tasks=new_tasks)

    def _check_heartbeats(self):
        while True:
            now = datetime.utcnow()
            for eid, executor in list(self.executors.items()):
                if (now - executor.last_heartbeat).total_seconds() > self.HEARTBEAT_TIMEOUT:
                    executor.status = "offline"
                    self._recover_executor_tasks(eid)
            time.sleep(10)

    def get_available_executors(self) -> list[ExecutorInfo]:
        return [e for e in self.executors.values() if e.status in ("idle", "busy")]
```

#### 5.1.4 进度监控模块

```python
class ProgressMonitor:
    def get_task_progress(self, task_id: str) -> TaskProgressSummary:
        task = self.db.get_task(task_id)
        completed = sum(1 for s in task.subtasks if s.status == "completed")
        downloaded = sum(s.downloaded_bytes for s in task.subtasks)
        total = task.total_size

        speeds = []
        for s in task.subtasks:
            if s.status == "downloading" and s.speed_bps:
                speeds.append(s.speed_bps)

        current_speed = sum(speeds) if speeds else 0
        remaining_bytes = total - downloaded
        eta_seconds = remaining_bytes / current_speed if current_speed > 0 else None

        return TaskProgressSummary(
            task_id=task_id,
            repo_id=task.repo_id,
            total_files=task.total_files,
            completed_files=completed,
            total_bytes=total,
            downloaded_bytes=downloaded,
            progress_percent=downloaded / total * 100 if total > 0 else 0,
            current_speed_bps=current_speed,
            eta_seconds=eta_seconds,
            executor_count=len(set(s.executor_id for s in task.subtasks if s.executor_id)),
            subtasks=[self._subtask_progress(s) for s in task.subtasks],
        )
```

#### 5.1.5 数据组装模块

```python
class DataAssemblyService:
    def on_file_completed(self, subtask: FileSubTask, file_path: str):
        target_path = os.path.join(subtask.task.target_dir, subtask.filename)
        os.makedirs(os.path.dirname(target_path), exist_ok=True)

        if subtask.executor and subtask.executor.hostname == self.controller_hostname:
            shutil.move(file_path, target_path)
        else:
            self._receive_from_executor(subtask, file_path, target_path)

        self._verify_file(target_path, subtask.expected_sha256, subtask.file_size)
        subtask.status = "completed"
        subtask.local_path = target_path
        self.db.update(subtask)

    def _verify_file(self, path: str, expected_sha256: str, expected_size: int):
        actual_size = os.path.getsize(path)
        if actual_size != expected_size:
            raise IntegrityError(f"Size mismatch: expected {expected_size}, got {actual_size}")

        if expected_sha256:
            actual_sha256 = self._compute_sha256(path)
            if actual_sha256 != expected_sha256:
                raise IntegrityError(
                    f"SHA256 mismatch: expected {expected_sha256}, got {actual_sha256}"
                )

    def verify_task_completion(self, task: DownloadTask) -> bool:
        index_file = os.path.join(task.target_dir, "model.safetensors.index.json")
        if os.path.exists(index_file):
            with open(index_file) as f:
                index = json.load(f)
            expected_total = index["metadata"]["total_size"]
            shard_files = set(index["weight_map"].values())
            actual_total = sum(
                os.path.getsize(os.path.join(task.target_dir, sf))
                for sf in shard_files
                if os.path.exists(os.path.join(task.target_dir, sf))
            )
            return actual_total == expected_total

        all_files_exist = all(
            os.path.exists(os.path.join(task.target_dir, s.filename))
            for s in task.subtasks
        )
        return all_files_exist
```

---

## 6. 下载执行器 (Executor) 详细设计

### 6.1 执行器架构

```
┌─────────────────────────────────────────────┐
│                  Executor                    │
│                                              │
│  ┌────────────┐  ┌──────────────────────┐   │
│  │  Controller │  │   Download Engine    │   │
│  │  Client     │  │                      │   │
│  │             │  │  ┌────────────────┐  │   │
│  │ - Register  │  │  │ Task Runner    │  │   │
│  │ - Heartbeat │  │  │  (per file)    │  │   │
│  │ - Report    │  │  └───────┬────────┘  │   │
│  │ - Transfer  │  │          │            │   │
│  └────────────┘  │  ┌───────┴────────┐  │   │
│                   │  │ Chunk Workers  │  │   │
│  ┌────────────┐  │  │  (N threads)   │  │   │
│  │  Local     │  │  └────────────────┘  │   │
│  │  Storage   │  │                      │   │
│  │  Manager   │  │  ┌────────────────┐  │   │
│  │            │  │  │ SHA256 Checker │  │   │
│  └────────────┘  │  └────────────────┘  │   │
│                   └──────────────────────┘   │
└─────────────────────────────────────────────┘
```

### 6.2 多线程分块下载引擎

```python
class ChunkedDownloader:
    def __init__(self, max_workers: int = 8, chunk_size: int = 256 * 1024 * 1024):
        self.max_workers = max_workers
        self.chunk_size = chunk_size  # 256MB per chunk

    def download_file(self, url: str, target_path: str, expected_size: int,
                      expected_sha256: str = None,
                      headers: dict = None,
                      progress_callback: Callable = None) -> DownloadResult:

        state = self._load_state(target_path)
        if state and state["sha256"] == expected_sha256:
            if self._verify_local(target_path, expected_sha256, expected_size):
                return DownloadResult(status="already_complete", path=target_path)

        temp_dir = target_path + ".parts"
        os.makedirs(temp_dir, exist_ok=True)

        chunk_ranges = self._calculate_chunks(expected_size, state.get("completed_chunks", []))
        completed_bytes = state.get("completed_bytes", 0)

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {}
            for i, (start, end) in enumerate(chunk_ranges):
                chunk_file = os.path.join(temp_dir, f"chunk_{i:05d}")
                futures[pool.submit(
                    self._download_chunk, url, chunk_file, start, end, headers
                )] = (i, start, end)

            for future in as_completed(futures):
                chunk_idx, start, end = futures[future]
                future.result()
                completed_bytes += (end - start + 1)
                self._save_state(target_path, {
                    "sha256": expected_sha256,
                    "completed_bytes": completed_bytes,
                    "total_size": expected_size,
                    "completed_chunks": [*state.get("completed_chunks", []), chunk_idx],
                })
                if progress_callback:
                    progress_callback(completed_bytes, expected_size)

        self._assemble_chunks(temp_dir, target_path, len(chunk_ranges))
        shutil.rmtree(temp_dir, ignore_errors=True)

        if expected_sha256:
            actual = self._compute_sha256(target_path)
            if actual != expected_sha256:
                raise IntegrityError(f"SHA256 mismatch: {actual} != {expected_sha256}")

        self._clear_state(target_path)
        return DownloadResult(status="completed", path=target_path)

    def _calculate_chunks(self, file_size: int,
                          completed_chunks: list[int]) -> list[tuple[int, int]]:
        chunks = []
        for start in range(0, file_size, self.chunk_size):
            end = min(start + self.chunk_size - 1, file_size - 1)
            chunk_idx = start // self.chunk_size
            if chunk_idx not in completed_chunks:
                chunks.append((start, end))
        return chunks

    def _download_chunk(self, url: str, chunk_file: str,
                        start: int, end: int, headers: dict = None):
        hdrs = {**(headers or {}), "Range": f"bytes={start}-{end}"}
        response = requests.get(url, headers=hdrs, stream=True, timeout=300)
        response.raise_for_status()
        with open(chunk_file, "wb") as f:
            for data in response.iter_content(chunk_size=10 * 1024 * 1024):
                f.write(data)

    def _assemble_chunks(self, temp_dir: str, target_path: str, num_chunks: int):
        with open(target_path, "wb") as out:
            for i in range(num_chunks):
                chunk_file = os.path.join(temp_dir, f"chunk_{i:05d}")
                with open(chunk_file, "rb") as inp:
                    shutil.copyfileobj(inp, out)
```

### 6.3 执行器主循环

```python
class Executor:
    def __init__(self, controller_url: str, executor_id: str = None):
        self.controller_url = controller_url
        self.executor_id = executor_id or str(uuid4())
        self.downloader = ChunkedDownloader(max_workers=8)
        self.active_tasks: dict[str, asyncio.Task] = {}
        self.heartbeat_interval = 10

    async def start(self):
        await self._register()
        asyncio.create_task(self._heartbeat_loop())
        asyncio.create_task(self._task_poll_loop())

    async def _register(self):
        resp = requests.post(f"{self.controller_url}/api/executors/register", json={
            "executor_id": self.executor_id,
            "hostname": socket.gethostname(),
            "ip": self._get_local_ip(),
            "max_workers": self.downloader.max_workers,
            "bandwidth_mbps": self._estimate_bandwidth(),
            "local_storage_path": self.storage_path,
        })
        resp.raise_for_status()

    async def _heartbeat_loop(self):
        while True:
            progress = self._collect_progress()
            try:
                resp = requests.post(
                    f"{self.controller_url}/api/executors/{self.executor_id}/heartbeat",
                    json={"progress": progress}
                )
                if resp.status_code == 200:
                    new_tasks = resp.json().get("tasks", [])
                    for task_info in new_tasks:
                        self._start_download(task_info)
            except Exception as e:
                log.error(f"Heartbeat failed: {e}")
            await asyncio.sleep(self.heartbeat_interval)

    async def _task_poll_loop(self):
        while True:
            try:
                resp = requests.get(
                    f"{self.controller_url}/api/executors/{self.executor_id}/tasks"
                )
                tasks = resp.json().get("tasks", [])
                for task_info in tasks:
                    if task_info["subtask_id"] not in self.active_tasks:
                        self._start_download(task_info)
            except Exception as e:
                log.error(f"Task poll failed: {e}")
            await asyncio.sleep(5)

    def _start_download(self, task_info: dict):
        subtask_id = task_info["subtask_id"]
        if subtask_id in self.active_tasks:
            return
        coro = self._download_subtask(task_info)
        self.active_tasks[subtask_id] = asyncio.create_task(coro)

    async def _download_subtask(self, task_info: dict):
        url = f"https://huggingface.co/{task_info['repo_id']}/resolve/{task_info['revision']}/{task_info['filename']}"
        headers = {}
        if task_info.get("token"):
            headers["Authorization"] = f"Bearer {task_info['token']}"

        target = os.path.join(self.storage_path, task_info["subtask_id"], task_info["filename"])
        os.makedirs(os.path.dirname(target), exist_ok=True)

        def on_progress(downloaded: int, total: int):
            self._report_progress(task_info["subtask_id"], downloaded, total)

        try:
            result = await asyncio.to_thread(
                self.downloader.download_file,
                url, target,
                expected_size=task_info["file_size"],
                expected_sha256=task_info.get("sha256"),
                headers=headers,
                progress_callback=on_progress,
            )
            await self._transfer_to_controller(task_info["subtask_id"], target)
            await self._report_completed(task_info["subtask_id"], target)
        except Exception as e:
            await self._report_failed(task_info["subtask_id"], str(e))
        finally:
            self.active_tasks.pop(task_info["subtask_id"], None)
```

---

## 7. Web UI 详细设计

### 7.1 页面结构

```
┌────────────────────────────────────────────────────┐
│  🔍 HuggingFace Distributed Downloader              │
├────────────────────────────────────────────────────┤
│                                                     │
│  [搜索页]  [下载任务]  [执行器管理]  [系统设置]      │
│                                                     │
│  ┌─ 搜索页 ─────────────────────────────────────┐  │
│  │                                               │  │
│  │  搜索: [_____________________________] [搜索] │  │
│  │  筛选: [作者▼] [任务▼] [排序▼] [数量▼]       │  │
│  │                                               │  │
│  │  ┌──────────────────────────────────────────┐ │  │
│  │  │ deepseek-ai/DeepSeek-V3                  │ │  │
│  │  │ Downloads: 1.2M | Likes: 5.2K            │ │  │
│  │  │ Tags: pytorch, text-generation, fp8      │ │  │
│  │  │ Pipeline: text-generation                │ │  │
│  │  │                         [查看详情] [下载] │ │  │
│  │  ├──────────────────────────────────────────┤ │  │
│  │  │ deepseek-ai/DeepSeek-R1                  │ │  │
│  │  │ ...                                      │ │  │
│  │  └──────────────────────────────────────────┘ │  │
│  └───────────────────────────────────────────────┘  │
│                                                     │
│  ┌─ 模型详情弹窗 ───────────────────────────────┐  │
│  │  deepseek-ai/DeepSeek-V3                     │  │
│  │  文件列表:                                    │  │
│  │  ☑ model-00001-of-00163.safetensors  4.3GB   │  │
│  │  ☑ model-00002-of-00163.safetensors  4.3GB   │  │
│  │  ☑ config.json                       1.2KB   │  │
│  │  ☑ tokenizer.json                    8.5MB   │  │
│  │  ... (共 163 + 配置文件)                      │  │
│  │                                               │  │
│  │  存储路径: [/data/models/DeepSeek-V3]         │  │
│  │  Token:    [hf_xxxxxxxxxxxx        ]          │  │
│  │                                               │  │
│  │  总大小: 689 GB | 预估时间: 2h 30m            │  │
│  │                            [取消] [开始下载]   │  │
│  └───────────────────────────────────────────────┘  │
│                                                     │
│  ┌─ 下载任务页 ─────────────────────────────────┐  │
│  │                                               │  │
│  │  ┌─────────────────────────────────────────┐  │  │
│  │  │ deepseek-ai/DeepSeek-V3          [暂停] │  │  │
│  │  │ ██████████████████░░░░░░░░░ 67.3%       │  │  │
│  │  │ 463.8 GB / 689 GB | 速度: 125 MB/s      │  │  │
│  │  │ ETA: 0h 30m 12s | 执行器: 5/6           │  │  │
│  │  │                                         │  │  │
│  │  │ ▼ 文件详情 (执行器视图)                   │  │  │
│  │  │ Executor-1 ██████████████████ 100% ✅    │  │  │
│  │  │   model-00001  4.3GB ✅                   │  │  │
│  │  │   model-00002  4.3GB ✅                   │  │  │
│  │  │   model-00003  2.1GB ████████░░ 80%      │  │  │
│  │  │ Executor-2 ████████████░░░░░░ 72%        │  │  │
│  │  │   model-00050  4.3GB ████████░░ 78%      │  │  │
│  │  │   model-00051  4.3GB ░░░░░░░░░░  0%      │  │  │
│  │  │ ...                                     │  │  │
│  │  └─────────────────────────────────────────┘  │  │
│  └───────────────────────────────────────────────┘  │
│                                                     │
│  ┌─ 执行器管理页 ───────────────────────────────┐  │
│  │                                               │  │
│  │  在线执行器: 5 | 离线: 1 | 总计: 6           │  │
│  │                                               │  │
│  │  ┌──────┬────────┬──────┬─────┬──────┬────┐ │  │
│  │  │ ID   │ 主机名  │ 状态  │任务数│ 带宽  │心跳│ │  │
│  │  ├──────┼────────┼──────┼─────┼──────┼────┤ │  │
│  │  │ E-01 │ node-1 │ 🟢在线│  3  │100Mbps│ 2s │ │  │
│  │  │ E-02 │ node-2 │ 🟢在线│  3  │100Mbps│ 5s │ │  │
│  │  │ E-03 │ node-3 │ 🔴离线│  -  │  -    │ 5m │ │  │
│  │  └──────┴────────┴──────┴─────┴──────┴────┘ │  │
│  └───────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────┘
```

### 7.2 实时进度推送 (WebSocket)

```python
# Controller 端
class ProgressWebSocket:
    async def broadcast_progress(self, task_id: str):
        progress = self.progress_monitor.get_task_progress(task_id)
        msg = {
            "type": "progress_update",
            "task_id": task_id,
            "data": {
                "total_bytes": progress.total_bytes,
                "downloaded_bytes": progress.downloaded_bytes,
                "progress_percent": progress.progress_percent,
                "current_speed_bps": progress.current_speed_bps,
                "eta_seconds": progress.eta_seconds,
                "completed_files": progress.completed_files,
                "total_files": progress.total_files,
                "executors": [
                    {
                        "executor_id": s.executor_id,
                        "filename": s.filename,
                        "status": s.status,
                        "downloaded_bytes": s.downloaded_bytes,
                        "file_size": s.file_size,
                        "speed_bps": s.speed_bps,
                    }
                    for s in progress.subtasks
                ],
            }
        }
        await self.manager.broadcast(task_id, json.dumps(msg))
```

---

## 8. 任务调度与负载均衡

### 8.1 任务分配策略

```
输入: 文件列表 F = [f1, f2, ..., fN], 执行器列表 E = [e1, e2, ..., eM]

策略: 加权轮询 (Weighted Round-Robin)

1. 按文件大小降序排列 F
2. 按执行器带宽权重 W(ei) 计算分配比例
3. 大文件优先分配，保证每个执行器总下载量均衡

算法:
  Sort F by size DESC
  Sort E by weight DESC (weight = bandwidth * max_workers)
  For each file f in F:
    Choose executor e with minimum current_assigned_bytes
    Assign f to e
```

### 8.2 动态重平衡

```python
class LoadBalancer:
    def rebalance(self, task: DownloadTask):
        pending = [s for s in task.subtasks if s.status == "pending"]
        active_executors = self.executor_manager.get_available_executors()

        if not active_executors or not pending:
            return

        current_load = {e.id: 0 for e in active_executors}
        for s in task.subtasks:
            if s.executor_id in current_load and s.status in ("assigned", "downloading"):
                current_load[s.executor_id] += max(0, s.file_size - s.downloaded_bytes)

        for subtask in sorted(pending, key=lambda s: s.file_size, reverse=True):
            min_executor = min(active_executors, key=lambda e: current_load[e.id])
            self._assign_subtask(subtask, min_executor)
            current_load[min_executor.id] += subtask.file_size
```

### 8.3 故障恢复

```
执行器离线处理:
1. 心跳超时 → 标记为 offline
2. 该执行器的 assigned/downloading 任务 → 标记为 pending
3. 已部分下载的文件:
   - 如果本地有 .incomplete 状态 → 可分配给新执行器继续（需传输临时文件）
   - 简化策略：重新分配给新执行器从头下载（断点续传在单执行器内保证）
4. 触发重平衡
```

---

## 9. 数据传输与组装

### 9.1 文件传输方案

| 方案 | 适用场景 | 优点 | 缺点 |
|------|---------|------|------|
| **HTTP multipart upload** | 通用 | 简单易实现 | 控制器需暴露端口 |
| **rsync** | 大文件/共享网络 | 增量传输、断点续传 | 需要rsync |
| **共享存储 (NFS/S3)** | 云环境 | 无需传输 | 需要共享存储 |
| **控制器拉取** | 安全 | 执行器无需暴露端口 | 控制器主动连接 |

**推荐方案：控制器拉取 + 支持可插拔存储后端**

```python
class StorageBackend(Protocol):
    def upload(self, local_path: str, remote_path: str) -> None: ...
    def download(self, remote_path: str, local_path: str) -> None: ...
    def exists(self, remote_path: str) -> bool: ...
    def delete(self, remote_path: str) -> None: ...

class LocalStorageBackend(StorageBackend):
    def upload(self, local_path: str, remote_path: str):
        shutil.copy2(local_path, remote_path)

class S3StorageBackend(StorageBackend):
    def upload(self, local_path: str, remote_path: str):
        self.client.upload_file(local_path, self.bucket, remote_path)

class SSHStorageBackend(StorageBackend):
    def upload(self, local_path: str, remote_path: str):
        subprocess.run(["rsync", "-avz", "--progress", local_path, f"{self.host}:{remote_path}"])
```

### 9.2 文件组装流程

```
Executor 完成 SHA256 校验后:
  1. 通知 Controller 文件已就绪
  2. Controller 指定传输方式（拉取 / 接收上传）
  3. 传输到目标目录 target_dir/{filename}
  4. Controller 再次验证 SHA256
  5. 更新任务状态为 completed
```

---

## 10. 完整性与一致性校验

### 10.1 多层校验机制

```
Layer 1: 执行器端 — 下载完成后
  - 文件大小校验: os.path.getsize() == expected_size
  - SHA256 校验: hashlib.sha256(file) == expected_sha256 (from HF LFS metadata)

Layer 2: 传输校验
  - HTTP 传输使用 Content-MD5 头
  - rsync 自带校验

Layer 3: 控制器端 — 接收完成后
  - 再次验证文件大小和 SHA256

Layer 4: 任务完成校验
  - 验证所有文件存在
  - 如果存在 model.safetensors.index.json:
    - 验证 metadata.total_size 与实际文件总大小一致
    - 验证 weight_map 中引用的所有文件存在
  - 验证 config.json 等配置文件完整
```

### 10.2 校验实现

```python
def compute_sha256(file_path: str, chunk_size: int = 8 * 1024 * 1024) -> str:
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(chunk_size):
            sha256.update(chunk)
    return sha256.hexdigest()

def verify_model_integrity(target_dir: str, file_list: list[FileEntry]) -> IntegrityReport:
    errors = []
    total_expected = 0
    total_actual = 0

    for entry in file_list:
        path = os.path.join(target_dir, entry.name)
        if not os.path.exists(path):
            errors.append(f"MISSING: {entry.name}")
            continue

        size = os.path.getsize(path)
        total_actual += size
        total_expected += entry.size

        if size != entry.size:
            errors.append(f"SIZE_MISMATCH: {entry.name} expected={entry.size} actual={size}")

        if entry.sha256:
            actual_sha256 = compute_sha256(path)
            if actual_sha256 != entry.sha256:
                errors.append(f"SHA256_MISMATCH: {entry.name}")

    index_path = os.path.join(target_dir, "model.safetensors.index.json")
    if os.path.exists(index_path):
        with open(index_path) as f:
            index = json.load(f)
        if total_actual != index["metadata"]["total_size"]:
            errors.append(
                f"TOTAL_SIZE_MISMATCH: expected={index['metadata']['total_size']} "
                f"actual={total_actual}"
            )

    return IntegrityReport(valid=len(errors) == 0, errors=errors,
                           total_expected=total_expected, total_actual=total_actual)
```

---

## 11. 进度监控与 ETA 估算

### 11.1 速度计算

```python
class SpeedCalculator:
    def __init__(self, window_size: int = 10):
        self.samples: deque[tuple[float, int]] = deque(maxlen=window_size)

    def add_sample(self, timestamp: float, bytes_downloaded: int):
        self.samples.append((timestamp, bytes_downloaded))

    def get_speed_bps(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        t1, b1 = self.samples[0]
        t2, b2 = self.samples[-1]
        dt = t2 - t1
        if dt <= 0:
            return 0.0
        return (b2 - b1) / dt
```

### 11.2 ETA 估算

```
ETA = remaining_bytes / aggregate_speed

aggregate_speed = Σ(executor_i.current_speed) for all active executors

remaining_bytes = total_bytes - Σ(downloaded_bytes) for all subtasks

平滑处理:
  - 使用滑动窗口（最近 10 个采样点）计算速度
  - 排除异常值（速度为 0 或暴增的采样）
  - 首次估算在下载开始 30 秒后显示，避免初期不准确
```

---

## 12. 技术选型

### 12.1 后端

| 模块 | 技术选择 | 理由 |
|------|---------|------|
| **语言** | Python 3.11+ | 生态成熟，HF SDK 为 Python |
| **Web 框架** | FastAPI | 异步、高性能、自动 OpenAPI 文档 |
| **WebSocket** | FastAPI WebSocket | 与 REST API 统一框架 |
| **数据库** | SQLite (开发) / PostgreSQL (生产) | 轻量 + 可扩展 |
| **ORM** | SQLAlchemy 2.0 | 类型安全、异步支持 |
| **任务队列** | 内置调度器 | 简化部署，避免引入 Redis/RabbitMQ |
| **HTTP 客户端** | httpx | 支持异步、流式、Range 请求 |
| **HF SDK** | huggingface_hub | 官方 SDK，搜索/元数据/下载 |

### 12.2 前端

| 技术 | 选择 | 理由 |
|------|------|------|
| **框架** | Vue 3 + TypeScript | 组件化、响应式 |
| **UI 库** | Element Plus | 成熟的 UI 组件库 |
| **图表** | ECharts | 进度可视化、速度曲线 |
| **实时通信** | WebSocket (native) | 实时进度推送 |
| **构建** | Vite | 快速开发体验 |

### 12.3 部署

| 组件 | 方式 |
|------|------|
| Controller | Docker / 直接运行 |
| Executor | Docker / 直接运行 / systemd |
| UI | Nginx 静态托管 / 开发时 Vite dev server |

---

## 13. API 接口定义

### 13.1 搜索与模型 API

```
GET  /api/models/search?query=deepseek&sort=downloads&limit=20
     Response: { models: [{ id, author, downloads, likes, tags, pipeline_tag, ... }] }

GET  /api/models/{repo_id}/info?revision=main
     Response: { id, siblings: [{ name, size, sha256 }], total_size, ... }

GET  /api/models/{repo_id}/files?revision=main
     Response: { files: [{ name, size, sha256, is_lfs }] }
```

### 13.2 下载任务 API

```
POST /api/tasks
     Body: { repo_id, revision, target_dir, token?, file_filter? }
     Response: { task_id, total_files, total_size }

GET  /api/tasks
     Response: { tasks: [{ task_id, repo_id, status, progress_percent, ... }] }

GET  /api/tasks/{task_id}
     Response: { task_id, repo_id, status, progress, subtasks, eta, ... }

POST /api/tasks/{task_id}/pause
POST /api/tasks/{task_id}/resume
POST /api/tasks/{task_id}/cancel
DELETE /api/tasks/{task_id}
```

### 13.3 执行器 API

```
POST /api/executors/register
     Body: { executor_id, hostname, ip, max_workers, bandwidth_mbps, local_storage_path }
     Response: { status: "ok", executor_id }

POST /api/executors/{executor_id}/heartbeat
     Body: { progress: [{ subtask_id, downloaded_bytes, speed_bps, status }] }
     Response: { tasks: [{ subtask_id, ... }] }

GET  /api/executors/{executor_id}/tasks
     Response: { tasks: [{ subtask_id, repo_id, filename, file_size, sha256, ... }] }

POST /api/executors/{executor_id}/tasks/{subtask_id}/complete
     Body: { sha256: "abc...", file_size: 12345 }
     Response: { status: "ok" }

POST /api/executors/{executor_id}/tasks/{subtask_id}/failed
     Body: { error: "..." }

GET  /api/executors
     Response: { executors: [{ id, hostname, status, active_tasks, ... }] }
```

### 13.4 文件传输 API

```
POST /api/transfer/upload/{task_id}/{subtask_id}
     Content-Type: multipart/form-data
     Body: file binary
     Response: { status: "ok", sha256_verified: true }

POST /api/transfer/ready/{task_id}/{subtask_id}
     Body: { file_path: "/local/path/file", file_size: 123, sha256: "abc" }
     Response: { status: "ok" }
     (控制器随后主动拉取文件)
```

### 13.5 WebSocket

```
WS   /ws/tasks/{task_id}/progress
     Messages (Server → Client):
     {
       "type": "progress_update",
       "data": {
         "downloaded_bytes": 463800000000,
         "total_bytes": 689000000000,
         "progress_percent": 67.3,
         "current_speed_bps": 131072000,
         "eta_seconds": 1812,
         "completed_files": 109,
         "total_files": 163,
         "executors": [...]
       }
     }
```

---

## 14. 部署方案

### 14.1 Controller 部署

```yaml
# docker-compose.controller.yml
version: "3.8"
services:
  controller:
    build:
      context: .
      dockerfile: Dockerfile.controller
    ports:
      - "8080:8080"     # REST API
      - "8081:8081"     # WebSocket
    volumes:
      - ./data:/data         # 下载目标目录
      - ./config:/app/config # 配置文件
    environment:
      - DATABASE_URL=sqlite:///data/downloader.db
      - HF_TOKEN=${HF_TOKEN}
      - STORAGE_TYPE=local   # local | s3 | ssh
      - S3_BUCKET=${S3_BUCKET}
    restart: unless-stopped

  ui:
    build:
      context: ./ui
      dockerfile: Dockerfile
    ports:
      - "3000:80"
    depends_on:
      - controller
```

### 14.2 Executor 部署

```yaml
# docker-compose.executor.yml
version: "3.8"
services:
  executor:
    build:
      context: .
      dockerfile: Dockerfile.executor
    environment:
      - CONTROLLER_URL=http://controller-host:8080
      - EXECUTOR_ID=executor-node-1
      - MAX_WORKERS=8
      - CHUNK_SIZE=268435456  # 256MB
    volumes:
      - /data/downloads:/tmp/downloads  # 本地临时存储
    restart: unless-stopped
```

### 14.3 Executor 快速启动脚本

```bash
#!/bin/bash
# start_executor.sh
EXECUTOR_ID=${1:-$(hostname)}
CONTROLLER_URL=${2:-"http://controller:8080"}
MAX_WORKERS=${3:-8}

docker run -d \
  --name hf-downloader-executor \
  -e CONTROLLER_URL=$CONTROLLER_URL \
  -e EXECUTOR_ID=$EXECUTOR_ID \
  -e MAX_WORKERS=$MAX_WORKERS \
  -v /data/downloads:/tmp/downloads \
  --restart unless-stopped \
  hf-downloader:executor
```

---

## 15. 项目结构

```
hf-distributed-downloader/
├── controller/                    # 控制器
│   ├── main.py                   # 入口
│   ├── config.py                 # 配置
│   ├── api/                      # REST API
│   │   ├── routes/
│   │   │   ├── models.py         # HF 搜索/详情
│   │   │   ├── tasks.py          # 下载任务管理
│   │   │   ├── executors.py      # 执行器管理
│   │   │   └── transfer.py       # 文件传输
│   │   └── websocket.py          # WebSocket 进度推送
│   ├── services/
│   │   ├── hf_search.py          # HF API 封装
│   │   ├── task_scheduler.py     # 任务调度
│   │   ├── executor_manager.py   # 执行器管理
│   │   ├── load_balancer.py      # 负载均衡
│   │   ├── progress_monitor.py   # 进度监控
│   │   ├── data_assembly.py      # 数据组装
│   │   ├── integrity_check.py    # 完整性校验
│   │   └── storage.py            # 存储后端抽象
│   ├── models/
│   │   ├── database.py           # 数据库连接
│   │   ├── task.py               # 任务模型
│   │   ├── executor.py           # 执行器模型
│   │   └── subtask.py            # 子任务模型
│   ├── Dockerfile
│   └── requirements.txt
│
├── executor/                      # 下载执行器
│   ├── main.py                   # 入口
│   ├── config.py                 # 配置
│   ├── controller_client.py      # 控制器通信客户端
│   ├── download_engine.py        # 多线程分块下载引擎
│   ├── chunked_downloader.py     # 分块下载器
│   ├── resume_manager.py         # 断点续传管理
│   ├── file_transfer.py          # 文件传输客户端
│   ├── integrity.py              # 本地完整性校验
│   ├── Dockerfile
│   └── requirements.txt
│
├── ui/                           # 前端
│   ├── src/
│   │   ├── views/
│   │   │   ├── SearchView.vue    # 模型搜索
│   │   │   ├── TaskView.vue      # 下载任务列表
│   │   │   ├── TaskDetail.vue    # 任务详情/进度
│   │   │   ├── ExecutorView.vue  # 执行器管理
│   │   │   └── SettingsView.vue  # 系统设置
│   │   ├── components/
│   │   │   ├── ModelCard.vue     # 模型卡片
│   │   │   ├── FileList.vue      # 文件列表
│   │   │   ├── ProgressBar.vue   # 进度条
│   │   │   ├── SpeedChart.vue    # 速度曲线
│   │   │   └── ExecutorTable.vue # 执行器表格
│   │   ├── composables/
│   │   │   ├── useWebSocket.ts   # WebSocket 连接
│   │   │   └── useApi.ts         # API 调用
│   │   ├── App.vue
│   │   └── main.ts
│   ├── Dockerfile
│   ├── package.json
│   ├── tsconfig.json
│   └── vite.config.ts
│
├── shared/                       # 共享类型/常量
│   ├── types.py                  # 数据类型定义
│   └── constants.py              # 常量
│
├── tests/                        # 测试
│   ├── controller/
│   ├── executor/
│   └── integration/
│
├── docker-compose.controller.yml
├── docker-compose.executor.yml
├── design_document.md            # 本文档
└── README.md
```

---

## 附录 A: 关键数据流时序图

```
User          UI            Controller        Executor-1       Executor-2       HF CDN
 │             │                │                 │                │             │
 │──Search───>│                 │                 │                │             │
 │             │──HF API──────>│                 │                │             │
 │             │<─results──────│                 │                │             │
 │<─display────│               │                 │                │             │
 │             │               │                 │                │             │
 │──Download──>│               │                 │                │             │
 │             │──Create Task─>│                 │                │             │
 │             │               │──Fetch Files──>│                │             │
 │             │               │<─File List─────│                │             │
 │             │               │                 │                │             │
 │             │               │──Assign Files──>│(files 1-82)    │             │
 │             │               │──Assign Files──────────────>│(files 83-163)│
 │             │               │                 │                │             │
 │             │               │                 │──Range GET───>│             │
 │             │               │                 │<─chunk───────│             │
 │             │               │                 │                │──Range GET─>│
 │             │               │                 │                │<─chunk─────│
 │             │               │                 │                │             │
 │             │<─WS progress──│<──heartbeat─────│                │             │
 │             │<─WS progress──│<─────────────heartbeat──────────│             │
 │             │               │                 │                │             │
 │             │               │                 │──complete────>│             │
 │             │               │──verify+store──│                │             │
 │             │               │                 │                │──complete──>│
 │             │               │──verify+store───────────────│                │
 │             │               │                 │                │             │
 │             │<─WS done──────│                 │                │             │
 │<─done───────│               │                 │                │             │
```

---

## 附录 B: HuggingFace API 快速参考

| API | Endpoint | 用途 |
|-----|----------|------|
| 搜索模型 | `GET /api/models?search=xxx` | UI 搜索框 |
| 模型详情 | `GET /api/models/{id}` | 获取文件列表 |
| 文件树 | `GET /api/models/{id}/tree/main/` | 浏览文件 |
| 下载文件 | `GET /{id}/resolve/main/{file}` | 下载（支持 Range） |
| 文件元数据 | HEAD `/{id}/resolve/main/{file}` | 获取 SHA256/大小 |
| LFS 批量 | `POST /{id}.git/info/lfs/objects/batch` | LFS 元数据 |

**Python SDK 关键方法：**
```python
HfApi.list_models(search, author, sort, limit, filter, ...)
HfApi.model_info(repo_id, revision, files_metadata=True)
HfApi.list_repo_tree(repo_id, recursive=True)
hf_hub_download(repo_id, filename, local_dir, force_download, token)
snapshot_download(repo_id, local_dir, max_workers, allow_patterns, token)
get_hf_file_metadata(url)  # → commit_hash, etag, size
```
