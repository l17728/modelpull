# 11 — CLI 与 SDK 规范

> 角色：CLI / SDK 实现者；用户文档作者。
> 范围：`dlw` CLI 全部命令、Python SDK 接口、错误码、退出码、配置约定。

---

## 0. 设计原则

1. **声明式优先**：`dlw submit` 是声明"我要这个模型"，不是命令式调度
2. **Idempotent by default**：所有写命令带 `--idempotency-key`（默认基于 args 派生）
3. **机器可读 ⊕ 人可读**：`--output table|json|yaml`，JSON 是 stable contract
4. **POSIX 友好退出码**：0 成功，1 通用错误，2 用法错误，3-9 业务错误（详见 §6）
5. **环境变量优先级**：CLI flag > env > config file > default
6. **Streaming 友好**：`dlw watch` / `dlw events` 支持 `--follow`，stdout 行缓冲

---

## 1. 安装与配置

### 1.1 安装方式

```bash
# Linux/Mac（推荐）
curl -fsSL https://get.dlw.example.com/install.sh | bash

# Windows (PowerShell)
iwr -useb https://get.dlw.example.com/install.ps1 | iex

# pip
pip install dlw-cli

# Homebrew
brew install dlw

# 二进制包（offline 场景）
wget https://github.com/.../releases/dlw-v2.0.0-linux-amd64.tar.gz
```

### 1.2 配置文件

`~/.dlw/config.yaml`（XDG: `${XDG_CONFIG_HOME}/dlw/config.yaml`）：

```yaml
# 当前 active context
current_context: prod

contexts:
  prod:
    server: https://api.dlw.example.com
    tenant: team-a
  dev:
    server: http://localhost:8000
    tenant: default

# Token 由 oidc login 写入；不要手动编辑
auth:
  prod:
    access_token: <jwt>
    refresh_token: <jwt>
    expires_at: 2026-04-28T15:30:00Z
  dev:
    access_token: dev-bypass-token

defaults:
  storage_id: 5             # 默认 storage backend
  source_strategy: auto_balance
  priority: 1
  output: table             # table / json / yaml
  color: auto               # auto / always / never
```

### 1.3 环境变量

| 变量 | 等效 flag |
|------|----------|
| `DLW_SERVER` | `--server` |
| `DLW_TENANT` | `--tenant` |
| `DLW_TOKEN` | (覆盖 OIDC 登录态，CI 用) |
| `DLW_OUTPUT` | `--output` |
| `DLW_NO_COLOR` | `--color=never` |
| `DLW_CONFIG` | `-c` |

### 1.4 全局 flag

| Flag | 简写 | 默认 | 说明 |
|------|------|------|------|
| `--server` | | from config | API URL |
| `--tenant` | | from config | tenant slug |
| `--context` | | from config | switch context |
| `--output` | `-o` | table | table/json/yaml/wide |
| `--no-color` | | false | 禁色 |
| `--quiet` | `-q` | false | 仅输出关键结果 |
| `--verbose` | `-v` | false | 增加日志（可叠加 -vv -vvv） |
| `--config` | `-c` | `~/.dlw/config.yaml` | 配置文件 |
| `--help` | `-h` | | 帮助 |
| `--version` | | | 版本 |

---

## 2. 命令清单

### 2.1 鸟瞰

```
dlw login                           # 登录
dlw logout                          # 注销
dlw whoami                          # 当前用户/tenant
dlw context [list|use|current]      # 切换 server/tenant context

dlw submit <repo> [opts]            # 创建任务
dlw list [filters]                  # 列任务
dlw show <id>                       # 任务详情
dlw watch <id>                      # 实时跟随
dlw events <id> [--follow]          # 事件日志
dlw cancel <id>                     # 取消
dlw retry <id> [--subtasks ...]     # 重试失败子任务
dlw upgrade <id> --to <rev>         # 增量升级
dlw materialize <id> --to <path>    # 拉到本地

dlw search <query> [filters]        # 模型搜索
dlw info <repo> --revision <sha>    # 模型信息

dlw quota [show|usage]              # 配额查看
dlw exec [list|show|drain]          # 节点
dlw storage [list|create|delete]    # storage backend
dlw audit [search]                  # 审计

dlw template [list|apply]           # 任务模板
dlw config [get|set|edit]           # 配置

dlw admin tenant [...]              # 管理员：租户
dlw admin maintenance [enter|exit]  # 维护模式

dlw completion [bash|zsh|fish]      # shell 补全

dlw version                         # 版本
```

### 2.2 详细：`dlw login`

```
Usage: dlw login [OPTIONS]

OIDC PKCE Device Code 流程登录。打开浏览器（或显示 URL+code）完成认证。

OPTIONS:
  --context TEXT          要登录的 context（默认 current_context）
  --no-browser            不自动开浏览器，仅显示 URL+code
  --device-code           强制 device code flow（CI 友好）
  --token TEXT            直接使用静态 token（绕过 OIDC，dev only）

EXAMPLES:
  $ dlw login
  Opening browser for authentication...
  Logged in as alice@team.com (tenant: team-a)

  $ dlw login --device-code
  Visit: https://auth.example.com/device
  Enter code: ABCD-1234

  $ DLW_TOKEN=xxx dlw whoami      # CI 场景
```

### 2.3 详细：`dlw submit`

```
Usage: dlw submit REPO [OPTIONS]

创建下载任务。

ARGUMENTS:
  REPO                    格式 'org/model' 或 'org/model:revision'
                          revision 默认 'main'，会被解析为 sha 后锁定

OPTIONS:
  -r, --revision TEXT     Git sha 或 'main'/'master'（自动解析）
  -s, --storage ID        Storage backend ID 或 name
  --priority [low|normal|high|urgent]
                          默认 normal
  --strategy [auto|hf|modelscope|hf-mirror|fastest|custom]
                          源策略（详见 06 §1）
  --sources LIST          custom 时显式指定（逗号分隔）
  --files [core|all|glob:PATTERN]
                          文件过滤
  --upgrade-from REV      增量升级基线
  --bytes-limit GB        流量上限
  --dry-run               不创建，仅显示计划
  --wait                  阻塞直到完成
  --timeout DURATION      --wait 超时（如 30m）
  --idempotency-key UUID  幂等
  --simulation            模拟模式（不实际下载）
  --trust-non-hf-sha256   信任非 HF 源 sha256（需 admin 审批）

EXAMPLES:
  $ dlw submit deepseek-ai/DeepSeek-V3
  Resolved revision: abc123def4567890... (from 'main')
  Created task: 7e57a3f8-1234-...
  Estimated: 689 GB / 163 files
  Speed probe ETA: 8s

  $ dlw submit Qwen/Qwen3-72B --strategy fastest --priority high --wait
  Task: 7e57a3f8-...
  [▓▓▓▓▓▓▓▓░░] 78% · 1.2 GB/s · ETA 4m 12s
  ✓ Completed in 18m 24s

  $ dlw submit Qwen/Qwen3-72B:abc123 -o json
  {"id": "7e57a3f8-...", "status": "pending", ...}

EXIT CODES:
  0  Created (or completed if --wait)
  3  REPO_NOT_FOUND
  4  REPO_GATED (审批 ticket 已创建)
  5  QUOTA_EXCEEDED
  6  DUPLICATE_TASK
```

### 2.4 详细：`dlw list`

```
Usage: dlw list [OPTIONS]

OPTIONS:
  --status STATUS         过滤状态（可多选：--status downloading,verifying）
  --project NAME
  --created-after DATE    'today' / '7d' / ISO date
  --owner EMAIL
  --limit N               默认 50
  --sort COLUMN           created_at / priority / status / progress
  --watch                 SUBSCRIPTION 实时刷新（每次变更重绘）

EXAMPLES:
  $ dlw list --status downloading
  ID                                    STATUS       MODEL                          PROGRESS  AGE
  7e57a3f8-1234-...                     downloading  deepseek-ai/DeepSeek-V3        67%       12m
  bb22cc33-5678-...                     verifying    Qwen/Qwen3-72B-Instruct        92%       25m

  $ dlw list -o json | jq '.items[] | select(.priority == 3)'
```

### 2.5 详细：`dlw watch`

```
Usage: dlw watch ID [OPTIONS]

OPTIONS:
  --refresh-rate MS       刷新频率（默认 500ms，从 WS 接收）
  --no-tui                简化输出，无终端控制字符（CI 用）

EXAMPLES:
  $ dlw watch 7e57a3f8
  Task: deepseek-ai/DeepSeek-V3 @ abc123
  Status: downloading       Priority: ⭐⭐ Normal
  Progress: [▓▓▓▓▓▓▓░░░] 67% · 462 / 689 GB
  Speed:  1.2 GB/s         ETA: 18m 12s
  Files:  108 / 163 ✓     5 in flight    50 pending

  Sources                       Speed (EWMA)
   ModelScope    62%  428 GB     950 MB/s   ●●●●●●●●●● healthy
   HF Mirror     28%  193 GB     420 MB/s   ●●●●● healthy
   HuggingFace   10%   68 GB      85 MB/s   ⚠ throttled

  Press 'q' to quit, 'c' to cancel task
```

### 2.6 详细：`dlw materialize`

```
Usage: dlw materialize ID --to PATH [OPTIONS]

把已完成任务的文件从 storage 拉到本地工作目录。

OPTIONS:
  --to PATH               目标目录（必需）
  --link [hard|sym|copy]  hard=硬链接（默认）/ sym=软链 / copy=复制
  --files PATTERN         glob 过滤
  --hf-cache              使用 HF cache 路径布局（取代 --to）

EXAMPLES:
  $ dlw materialize 7e57a3f8 --to ./models/deepseek-v3
  Linking 163 files from s3://prod/team-a/.../ to ./models/deepseek-v3 ...
  ✓ Materialized 689 GB (hard links, 0 GB local)

  $ dlw materialize 7e57a3f8 --hf-cache
  Layout: ~/.cache/huggingface/hub/models--deepseek-ai--DeepSeek-V3/snapshots/abc123.../
  ✓ Materialized
  $ python -c "from transformers import AutoModelForCausalLM; AutoModelForCausalLM.from_pretrained('deepseek-ai/DeepSeek-V3')"  # 直接用
```

### 2.7 详细：`dlw search` / `dlw info`

```
Usage: dlw search QUERY [OPTIONS]

OPTIONS:
  --limit N               默认 20
  --pipeline TAG          text-generation / image-classification / ...
  --sort [downloads|likes|updated]

EXAMPLES:
  $ dlw search deepseek
  REPO                                ⭐ DOWNLOADS  TASK            COVERAGE
  deepseek-ai/DeepSeek-V3            12,500       text-gen        HF MS Mir
  deepseek-ai/DeepSeek-Coder-V2       8,200       text-gen        HF MS Mir
  ...
```

```
Usage: dlw info REPO --revision SHA

EXAMPLES:
  $ dlw info Qwen/Qwen3-72B --revision abc123def4567890abc123def4567890abc12345
  Repo:        Qwen/Qwen3-72B-Instruct
  Revision:    abc123def4567890abc123def4567890abc12345
  License:     apache-2.0
  Gated:       no
  Total size:  144.0 GB
  Files:       30
   ✓ model-00001-of-00030.safetensors  4.8 GB  HF MS Mir
   ✓ model-00002-of-00030.safetensors  4.8 GB  HF MS Mir
   ...
   ✓ tokenizer.json                    2.3 MB  HF
   ✓ config.json                       456 B   HF
```

### 2.8 详细：`dlw quota`

```
Usage: dlw quota [show|usage] [OPTIONS]

EXAMPLES:
  $ dlw quota
  Tenant: team-a
  Period: 2026-04-01 ~ 2026-04-30 (12 days remaining)

  Bytes (month)        ▓▓▓▓▓▓▓▓░░ 78%   39.0 / 50.0 TB
  Storage              ▓▓▓▓▓▓░░░░ 62%    3.2 / 5.1 TB
  Concurrent tasks     3 / 10

  Forecast end-of-month: 47.0 TB (within limit)

  $ dlw quota usage --from 2026-04-01 --group-by project
  PROJECT     BYTES_DOWNLOADED   TASKS
  research    28.4 TB            45
  inference   11.0 TB            18
  default      5.1 TB            12
```

### 2.9 详细：`dlw exec`

```
$ dlw exec list
HOST            EXECUTOR        STATUS    SCORE  TASKS  NIC%   DISK
host-01.local   host-01-w1      healthy   100    3      78     38%
host-01.local   host-01-w2      healthy   100    2      78     38%
host-04.local   host-04-w1      degraded  62     1      45     71%
host-05.local   host-05-w1      faulty    0      0      -      -

$ dlw exec drain host-04-w1
Draining host-04-w1...
  3 in-flight subtasks
  ✓ subtask 1/3 released
  ✓ subtask 2/3 completed
  ✓ subtask 3/3 released
host-04-w1 drained successfully (took 4m 12s)
```

### 2.10 详细：`dlw template`

```yaml
# templates/qwen-family.yaml
name: "Qwen3 family weekly snapshot"
storage: project-default
priority: normal
strategy: auto_balance
files: core
tasks:
  - repo: Qwen/Qwen3-72B-Instruct
    revision: latest_sha
  - repo: Qwen/Qwen3-32B-Instruct
    revision: latest_sha
```

```
$ dlw template apply templates/qwen-family.yaml
Resolving 'latest_sha' for each repo...
  Qwen/Qwen3-72B-Instruct -> abc123...
  Qwen/Qwen3-32B-Instruct -> def456...
Submitting 2 tasks...
  ✓ Qwen/Qwen3-72B-Instruct: 7e57a3f8-...
  ✓ Qwen/Qwen3-32B-Instruct: bb22cc33-...
```

---

## 3. 输出格式约定

### 3.1 Table

- 默认终端友好；颜色区分状态
- 列宽自适应；超长字段截断（光标移上去显示完整）
- `--output wide` 显示更多列

### 3.2 JSON

- **Stable contract**（v2.0 起，不许破坏性变更）
- 字段名 snake_case（与 OpenAPI 一致）
- 时间字段 ISO 8601 + Z
- bytes 用整数（不缩写为 GB）
- 顶层包裹：单对象返回 `{...}`；列表返回 `{items: [...], next_cursor?: ...}`

```bash
$ dlw show 7e57a3f8 -o json
{
  "id": "7e57a3f8-1234-4567-8901-abcdef012345",
  "tenant_id": 1,
  "repo_id": "deepseek-ai/DeepSeek-V3",
  "revision": "abc123def4567890abc123def4567890abc12345",
  "status": "downloading",
  "progress": {
    "files_total": 163,
    "files_completed": 108,
    "bytes_total": 740088332288,
    "bytes_downloaded": 495886823424,
    "eta_seconds": 1092
  },
  "trace_id": "c0ffee0123456789"
}
```

### 3.3 YAML

- 同 JSON 内容，YAML 格式
- 用于 ops 阅读

---

## 4. 错误码与退出码

### 4.1 退出码

| Code | 含义 | 例子 |
|------|------|------|
| 0 | 成功 |  |
| 1 | 通用错误（unexpected） | 网络异常、内部错误 |
| 2 | 用法错误（usage） | 参数缺失、格式错误 |
| 3 | 资源不存在 | REPO_NOT_FOUND, TASK_NOT_FOUND |
| 4 | 鉴权失败 / forbidden | UNAUTHENTICATED, FORBIDDEN, REPO_GATED |
| 5 | 配额 / 限流 | QUOTA_EXCEEDED, RATE_LIMITED |
| 6 | 状态冲突 | DUPLICATE_TASK, STALE_ASSIGNMENT |
| 7 | 上游降级 | UPSTREAM_DEGRADED |
| 8 | 用户取消 | Ctrl+C 触发 |
| 9 | 超时 | --timeout 触发 |

### 4.2 错误格式

stderr 输出：

```
Error: <human readable message>
Code:  MACHINE_READABLE_CODE
Trace: c0ffee0123456789
Help:  https://docs.dlw.example.com/errors/MACHINE_READABLE_CODE

Details:
  - field1: ...
  - field2: ...
```

`-o json` 时 stderr 输出：

```json
{
  "code": "QUOTA_EXCEEDED",
  "message": "Tenant monthly bytes quota exceeded",
  "trace_id": "...",
  "details": {"current": 12345, "limit": 10000}
}
```

---

## 5. 自动补全

```bash
$ dlw completion bash > /etc/bash_completion.d/dlw
$ dlw completion zsh > "${fpath[1]}/_dlw"
$ dlw completion fish > ~/.config/fish/completions/dlw.fish
```

支持：
- 子命令 / flag 名补全
- task ID 补全（从最近 50 个任务）
- repo_id 补全（基于历史输入）

---

## 6. Python SDK

### 6.1 安装

```bash
pip install dlw-sdk
```

### 6.2 同步用法

```python
from dlw import Client

# 从 ~/.dlw/config 读 context
client = Client.from_env()

# 显式
client = Client(server="https://api.dlw.example.com", token="...")

# 提交任务
task = client.tasks.submit(
    repo_id="deepseek-ai/DeepSeek-V3",
    revision="abc123def4567890abc123def4567890abc12345",
    storage_id=5,
    priority=2,
    source_strategy="auto_balance",
)
print(task.id, task.status)

# 查询
task = client.tasks.get(task_id)

# 同步等
def on_progress(task):
    print(f"{task.progress.files_completed}/{task.progress.files_total}")

task.wait(timeout=3600, on_progress=on_progress)

# 列
for task in client.tasks.list(status="downloading"):
    print(task.repo_id, task.progress.bytes_downloaded)

# 取消
client.tasks.cancel(task_id, reason="user_request")

# 增量升级
client.tasks.upgrade(old_task_id, to_revision="def456...", keep_old_files_for_days=7)

# 拉到本地（HF cache layout）
client.tasks.materialize(task_id, hf_cache=True)

# 与 transformers 直接互通
from transformers import AutoModelForCausalLM
client.tasks.materialize("7e57a3f8...", hf_cache=True)
model = AutoModelForCausalLM.from_pretrained("deepseek-ai/DeepSeek-V3")  # 命中本地
```

### 6.3 异步用法

```python
import asyncio
from dlw import AsyncClient

async def main():
    async with AsyncClient.from_env() as client:
        task = await client.tasks.submit(
            repo_id="Qwen/Qwen3-72B",
            revision="abc...",
        )

        async for evt in task.stream_events():
            print(evt.type, evt.message)
            if evt.type == "task.completed":
                break

asyncio.run(main())
```

### 6.4 SDK 类型签名（核心）

```python
class Client:
    @classmethod
    def from_env(cls, context: str | None = None) -> "Client": ...

    def __init__(self,
                 server: str,
                 token: str,
                 tenant: str | None = None,
                 timeout: float = 30.0,
                 retries: int = 3,
                 ): ...

    @property
    def tasks(self) -> "TasksAPI": ...
    @property
    def executors(self) -> "ExecutorsAPI": ...
    @property
    def models(self) -> "ModelsAPI": ...
    @property
    def quota(self) -> "QuotaAPI": ...
    @property
    def storage(self) -> "StorageAPI": ...
    @property
    def audit(self) -> "AuditAPI": ...


class TasksAPI:
    def submit(self,
               repo_id: str,
               revision: str,
               *,
               storage_id: int | None = None,
               priority: int = 1,
               source_strategy: SourceStrategy = "auto_balance",
               source_blacklist: list[str] | None = None,
               file_filter: FileFilter = "core_only",
               file_glob: str | None = None,
               upgrade_from_revision: str | None = None,
               download_bytes_limit: int | None = None,
               idempotency_key: str | None = None,
               trust_non_hf_sha256: bool = False,
               ) -> "DownloadTask": ...

    def get(self, task_id: str) -> "DownloadTask": ...

    def list(self,
             *,
             status: TaskStatus | list[TaskStatus] | None = None,
             project_id: int | None = None,
             limit: int = 50,
             cursor: str | None = None,
             ) -> Iterator["DownloadTask"]: ...

    def cancel(self, task_id: str, reason: str | None = None) -> None: ...

    def retry(self, task_id: str, subtask_ids: list[str] | None = None) -> None: ...

    def upgrade(self,
                old_task_id: str,
                to_revision: str,
                keep_old_files_for_days: int = 7,
                ) -> "DownloadTask": ...

    def materialize(self,
                    task_id: str,
                    *,
                    to: str | Path | None = None,
                    link: Literal["hard", "sym", "copy"] = "hard",
                    files: str | None = None,    # glob
                    hf_cache: bool = False,
                    ) -> Path: ...


class DownloadTask:
    id: str
    status: TaskStatus
    repo_id: str
    revision: str
    progress: TaskProgress
    # ...

    def wait(self,
             timeout: float | None = None,
             on_progress: Callable[["DownloadTask"], None] | None = None,
             poll_interval: float = 5.0,
             ) -> "DownloadTask": ...

    def stream_events(self) -> Iterator["TaskEvent"]: ...   # WS 包装

    def refresh(self) -> "DownloadTask": ...
```

### 6.5 异步 SDK 同形

```python
class AsyncClient:
    @classmethod
    def from_env(cls, ...): ...
    async def __aenter__(self): ...
    async def __aexit__(self, ...): ...

    @property
    def tasks(self) -> "AsyncTasksAPI": ...

class AsyncTasksAPI:
    async def submit(self, ...) -> "AsyncDownloadTask": ...
    # ... 同步版本的所有方法都有 async 等效

class AsyncDownloadTask(DownloadTask):
    async def wait(self, ...) -> "AsyncDownloadTask": ...
    async def stream_events(self) -> AsyncIterator["TaskEvent"]: ...
```

### 6.6 SDK 错误处理

```python
from dlw import errors as e

try:
    task = client.tasks.submit(repo_id="...", revision="main")
except e.InvalidRevision:
    # main → resolve to sha first
    info = client.models.info(repo_id="...", revision="main")
    task = client.tasks.submit(repo_id="...", revision=info.revision)
except e.QuotaExceeded as ex:
    print(f"Quota: {ex.metric} {ex.current}/{ex.limit}")
except e.RepoGated as ex:
    print(f"Approval ticket: {ex.approval_ticket_url}")
except e.UpstreamDegraded:
    # HF / source 全降，等等再试
    time.sleep(60)
    ...
```

### 6.7 SDK 测试 fixture

提供 `dlw.testing` 模块用 ddl 模拟：

```python
from dlw.testing import MockServer, MockTask

def test_my_pipeline(tmp_path):
    with MockServer() as server:
        server.expect_submit(repo_id="test/x").returns(MockTask(id="t1"))
        server.expect_complete("t1").after(seconds=1)

        client = Client(server=server.url, token="test")
        task = client.tasks.submit(repo_id="test/x", revision="abc..." * 5)
        task.wait(timeout=10)

        assert task.status == "completed"
```

---

## 7. CLI 与 SDK 的对应

| 操作 | CLI | SDK (sync) |
|------|-----|----------|
| 创建 | `dlw submit X --revision Y` | `client.tasks.submit(X, Y)` |
| 查询 | `dlw show ID` | `client.tasks.get(ID)` |
| 列 | `dlw list --status downloading` | `client.tasks.list(status="downloading")` |
| 取消 | `dlw cancel ID` | `client.tasks.cancel(ID)` |
| 跟随 | `dlw watch ID` | `task.stream_events()` |
| 拉本地 | `dlw materialize ID --to PATH` | `client.tasks.materialize(ID, to=PATH)` |
| 增量 | `dlw upgrade ID --to REV` | `client.tasks.upgrade(ID, to_revision=REV)` |
| 配额 | `dlw quota` | `client.quota.current()` |
| 节点 | `dlw exec list` | `client.executors.list()` |

CLI 内部就是用 SDK 实现，通过这种方式保证一致性。

---

## 8. 兼容承诺

### 8.1 SemVer

- v2.x.y：所有 v2.0 已发布的 CLI flag 和 SDK 公开方法不破坏
- v3.0.0：允许 break 但提前 6 个月废弃通知

### 8.2 公开稳定 API

**稳定**（v2.0 contract，不会破坏）：
- 全部命令名与 flag
- JSON 输出格式（顶层 keys + 值类型）
- SDK 公开方法签名（`client.tasks.*` 等）
- 退出码

**不稳定**：
- table 输出列宽 / 颜色（视觉调整）
- stderr 字符串（人读用）
- SDK 内部 `_*` 前缀方法

### 8.3 Deprecation policy

废弃流程：

1. v2.x：标 deprecated，输出 stderr 提示
2. v2.x+0.5：仍可用但 stderr 提示更醒目
3. v3.0：删除

---

## 9. CLI 实现技术栈

| 组件 | 选型 |
|------|------|
| 框架 | **Typer** (基于 Click + pydantic) |
| 进度 / 终端 | **Rich** |
| 配置 | **pydantic-settings** |
| OIDC | `oidc-client-py` 或 `authlib` |
| WS 跟随 | `websockets` |
| 打包分发 | `uv build` + GitHub Actions release |
| 跨平台 | macOS, Linux x86_64/arm64, Windows |

骨架代码：

```python
# dlw/__main__.py
import typer

app = typer.Typer(no_args_is_help=True, add_completion=True)

@app.callback()
def root(
    server: str = typer.Option(None, "--server", envvar="DLW_SERVER"),
    output: str = typer.Option("table", "-o", "--output"),
):
    """dlw — Distributed HuggingFace model downloader CLI"""
    ...

@app.command()
def submit(
    repo: str = typer.Argument(...),
    revision: str = typer.Option("main", "-r"),
    priority: str = typer.Option("normal"),
    wait: bool = typer.Option(False),
    output: str = typer.Option("table", "-o"),
):
    """Submit a download task."""
    client = get_client()
    ...

if __name__ == "__main__":
    app()
```

---

## 10. 与其他文档的链接

- API 协议：→ [02-protocol.md](./02-protocol.md)
- 多源策略：→ [06-platform-and-ecosystem.md](./06-platform-and-ecosystem.md) §1
- 测试：→ [07-test-plan.md](./07-test-plan.md)
- 前端 UX 对齐：→ [10-frontend-wireframes.md](./10-frontend-wireframes.md)
