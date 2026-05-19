# Getting Started — 用户手册

> **Status**: ✅ 可运行。Phase 1/2/3 已全部落地（多机下载、多源调度、多租户/RBAC、增量去重、CLI + Python SDK）。
> 本文教你**在本机把整套系统跑起来、用 CLI/SDK 真实下载一个 HF 模型**。
> 生产部署的差异见 §11；逐步可复现的运维 runbook 见
> [`docs/operator/local-deployment.md`](./operator/local-deployment.md)。

---

## 1. modelpull 是什么

一个**分布式 HuggingFace 模型权重下载系统**：控制器（controller）编排，多个下载器（executor）并行从 HF 拉取文件并写入对象存储。面向"大模型、多文件、多机、多源、多租户、要正确性和可运维"的场景。

**不适合**：单机小模型 —— 直接用 [`huggingface_hub.snapshot_download`](https://huggingface.co/docs/huggingface_hub) 就够了。

---

## 2. 架构与核心概念

### 2.1 数据流

```
HuggingFace ──(1) HTTP/HTTPS──▶ 控制器(反向代理, 注入并隐藏 HF token)
                                      │
                                      │ (2) 下载器经控制器代理流式拉字节
                                      ▼
                              下载器(executor) ──(3) S3 协议 multipart 上传──▶ 对象存储
                                      ▲                                            │
                                      └──(0) mTLS 注册 + poll/heartbeat──┐         ▼
                                                                控制器 ◀──┘   桶里出现模型文件
                                                                              key = {tenant}/{repo}/{rev}/{file}
```

- **(1) 下载用 HTTP**：文件从 HF 用 HTTP(S) 拉取，经**控制器反向代理**转发——下载器自身拿不到 HF token（INVARIANT 2）。
- **(3) 存储用 S3 协议**：下载器把字节用 **S3 multipart upload** 写入存储后端，边写边算 sha256 校验。
- 下载的**协议是 HTTP，存储的协议才是 S3**——这两件事不要混淆。

### 2.2 存储后端是可插拔的（`StorageBackend` 抽象）

控制器/下载器代码不关心对端是 AWS S3、阿里云 OBS、minio 还是别的 S3 兼容存储——都走同一个 `StorageBackend` 抽象（`backend_type`: `s3 / obs / minio / nfs / local`）。换存储 = 改一条数据库配置行，**不动代码**。

### 2.3 为什么本地用 minio（而不是直接写本地文件）

本地没有云 S3，于是用 [**minio**](https://min.io)（一个 S3 兼容的对象存储服务）在 `localhost:9000` 顶替"云 S3"的角色。**S3 协议照用，只是 S3 服务端是本地 minio 而非云厂商**。这样做的好处：

1. **开发=生产同一条代码路径**：本地和生产跑完全相同的上传代码，上生产只改 `endpoint_url` 一行配置，消除"本地能跑、上云就崩"的整类 bug。
2. **大文件流式 + multipart + sha 校验**：模型权重几十~几百 GB，S3 multipart 支持边拉边分片并发上传、断点续传、同流算 sha256——裸文件系统 `write()` 给不了这些语义。
3. **适配分布式**：多台下载器写进**同一个共享对象命名空间**（bucket），与"哪台机器下的"无关、网络可达、统一可寻址。
4. **增量去重依赖对象语义**：全局去重的"继承"用 S3 服务端 `copy_object` 零重下物化文件；refcount/不变量 14 都是对象存储概念。
5. **消费端解耦**：训练/推理任务用标准 S3 客户端直接读 bucket，不需要知道下载器在哪。
6. **测试零成本但真实**：本地 minio / CI 的 `moto`（内存模拟）都跑真实 S3 代码路径，无需云账号/凭证/出网费。

> 项目**也支持** `backend_type=local`（裸文件系统 + `os.link` 硬链，单机共享文件系统/增量去重场景用）。`s3`+minio 是为了上面 1/2/3/4 那些分布式/大文件/生产对等的理由；按部署形态取舍。

完整设计原理见 [README](../README.md) 的「软件架构」与「独特设计」两节。

---

## 3. 前置依赖

| 依赖 | 版本 | 说明 |
|---|---|---|
| Python | 3.12 | |
| `uv` | 0.11+ | 包/虚拟环境管理（`curl -LsSf https://astral.sh/uv/install.sh \| sh`） |
| PostgreSQL | 14+（实测 PG 18） | 监听 `localhost:5433`，库名 `dlw`，`postgres` 用户、trust/空密码（dev） |
| minio 二进制 | 任意近期版本 | 本地 S3 后端（[下载](https://min.io/download)；放进 PATH 或项目 `.tools/`） |
| 出网到 huggingface.co | — | 控制器反向代理需要真实拉 HF |

---

## 4. 安装

```bash
git clone https://github.com/l17728/modelpull && cd modelpull
uv sync                       # 安装依赖到 .venv
```

无需 `pip install dlw-cli`（PyPI 占位、未发布）。`dlw` / `dlw-executor` / `dlw-seed` 通过 `uv run <cmd>` 使用，或 `pip install -e .` 后进 PATH。

---

## 5. 本地部署快速开始

下面是最小可跑流程。**完整可复现 runbook（含 mTLS/CA/HTTPS 细节、排错矩阵）见
[`docs/operator/local-deployment.md`](./operator/local-deployment.md)**。

```bash
# 0. 准备 minio 二进制（示例放到项目 .tools/）
mkdir -p .tools && curl -sL -o .tools/minio https://dl.min.io/server/minio/release/linux-amd64/minio && chmod +x .tools/minio
#   Windows: .tools/minio.exe  ←  https://dl.min.io/server/minio/release/windows-amd64/minio.exe

# 1. 建库 + migration（库名 dlw；端口按你的 PG 调整）
psql -h localhost -p 5433 -U postgres -d postgres -c "CREATE DATABASE dlw"   # 已存在则忽略报错
uv run alembic upgrade head

# 2. 起 minio + 建桶
mkdir -p .run/minio-data .run/logs
MINIO_ROOT_USER=minioadmin MINIO_ROOT_PASSWORD=minioadmin \
  nohup .tools/minio server .run/minio-data --address 127.0.0.1:9000 --console-address 127.0.0.1:9001 \
  > .run/logs/minio.log 2>&1 &
uv run python -c "import boto3;boto3.client('s3',endpoint_url='http://127.0.0.1:9000',aws_access_key_id='minioadmin',aws_secret_access_key='minioadmin').create_bucket(Bucket='modelpull-dev')"

# 3. 预生成 dev CA（hostname=localhost，供 uvicorn HTTPS 用）
uv run python -c "
from pathlib import Path
from dlw.auth.ca import bootstrap_ca, ensure_server_cert
from dlw.auth.jwt_signing import bootstrap_keypair
d=Path('./.ca'); d.mkdir(mode=0o700, parents=True, exist_ok=True)
ca=bootstrap_ca(d); ensure_server_cert(ca,d,hostname='localhost'); bootstrap_keypair(d)
(d/'enrollment.token').write_text('local-enroll-token')
print('CA ready:', sorted(p.name for p in d.iterdir()))"

# 4. seed 租户/项目/用户/存储后端
uv run dlw-seed --demo
#   把 StorageBackend(1) 的 config 指向本地 minio（seed 的 ON CONFLICT 不覆盖既有行时手动修一次）
uv run python -c "
import asyncio,asyncpg,json
cfg=json.dumps({'bucket':'modelpull-dev','region':'us-east-1','endpoint_url':'http://127.0.0.1:9000','key_prefix':'phase1/'})
async def m():
 c=await asyncpg.connect(host='localhost',port=5433,user='postgres',password='',database='dlw')
 await c.execute(\"UPDATE storage_backends SET config_encrypted=\$1::bytea,region='us-east-1',backend_type='s3' WHERE id=1\",cfg.encode()); await c.close()
asyncio.run(m())"

# 5. 起控制器（HTTPS + 可选 mTLS，httptools 后端）
DLW_AUTH_DEV_MODE=true DLW_SYSTEM_ADMIN_TOKEN=local-admin-token \
DLW_ENROLLMENT_TOKEN=local-enroll-token DLW_CONTROLLER_HOSTNAME=localhost \
  nohup uv run uvicorn dlw.main:create_app --factory --host 127.0.0.1 --port 8000 \
  --http httptools --ssl-certfile ./.ca/server-cert.pem --ssl-keyfile ./.ca/server-key.pem \
  --ssl-ca-certs ./.ca/ca-cert.pem --ssl-cert-reqs 1 > .run/logs/controller.log 2>&1 &

# 6. 起下载器（mTLS 自动注册 + minio 上传）
mkdir -p .executor-certs
DLW_EXECUTOR_ID=local-host-worker-1 DLW_EXECUTOR_BEARER_TOKEN=unused \
DLW_EXECUTOR_CONTROLLER_URL=https://localhost:8000 DLW_EXECUTOR_ENROLLMENT_TOKEN=local-enroll-token \
DLW_EXECUTOR_EXECUTOR_CERT_DIR=./.executor-certs DLW_EXECUTOR_EXECUTOR_CA_BUNDLE=./.ca/ca-cert.pem \
DLW_EXECUTOR_S3_ENDPOINT_URL=http://127.0.0.1:9000 DLW_EXECUTOR_S3_REGION=us-east-1 \
AWS_ACCESS_KEY_ID=minioadmin AWS_SECRET_ACCESS_KEY=minioadmin AWS_DEFAULT_REGION=us-east-1 \
  nohup uv run dlw-executor --log-level INFO > .run/logs/executor.log 2>&1 &
```

---

## 6. 使用 CLI

把鉴权/CA/服务器地址装进一个可 `source` 的助手（每次刷新一个有效 1 小时的租户用户 JWT）：

```bash
cat > .run/dlw-env.sh <<'EOF'
export DLW_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export SSL_CERT_FILE="$DLW_REPO/.ca/ca-cert.pem"          # 让 httpx 信任 dev CA
export DLW_SERVER="https://localhost:8000"
export DLW_TOKEN="$(uv run python -c "from dlw.auth.principal import issue_system_jwt; print(issue_system_jwt(secret='dev-system-jwt-change-me', user_id=1, tenant_id=1, role='tenant_admin', project_ids=[]))" 2>/dev/null)"
EOF

source .run/dlw-env.sh

uv run dlw list                                  # 列任务（表格）
uv run dlw -o json list                          # 机器可读
uv run dlw submit org/model -r <40位hex-sha> -s 1   # 提交（会向 HF 枚举文件）
uv run dlw show   <task-id>
uv run dlw watch  <task-id>                       # 轮询到终态
uv run dlw cancel <task-id>
uv run dlw delete <task-id>                       # 仅终态任务可删
```

> 退出码遵循 POSIX（`0` 成功 / `2` 用法或缺 token / `3` 不存在 / `4` 鉴权 / `6` 状态冲突 / `8` Ctrl-C / `9` 超时）。详见 [`docs/operator/cli-sdk.md`](./operator/cli-sdk.md)。

---

## 7. 使用 Python SDK

导入路径是 `dlw.sdk`（monorepo：控制器占用顶层 `dlw` 包）。**SDK 没有 `--cacert`/`verify=` 选项**（MVP 限制），自签 CA 场景靠 `SSL_CERT_FILE` 环境变量让 httpx 信任。

```python
import os
os.environ["SSL_CERT_FILE"] = "/abs/path/to/.ca/ca-cert.pem"   # 自签 CA 才需要

from dlw.sdk import Client                # 同步
with Client(server="https://localhost:8000", token="<租户用户 JWT>") as c:
    t = c.tasks.submit(repo_id="org/model", revision="<40hex>", storage_id=1)
    t = t.wait(timeout=3600, on_progress=lambda x: print(x.status, x.files_done()))
    for task in c.tasks.list(status="downloading"):
        print(task.repo_id)

import asyncio                            # 异步（同形）
from dlw.sdk import AsyncClient
async def main():
    async with AsyncClient(server="https://localhost:8000", token="<JWT>") as c:
        t = await c.tasks.submit(repo_id="org/model", revision="<40hex>", storage_id=1)
        print((await t.refresh()).status)
asyncio.run(main())
```

错误是带类型的（`dlw.sdk.errors`：`NotFound/AuthError/QuotaExceeded/Conflict/Timeout/UsageError/ApiError`，均继承 `DlwError`）。

---

## 8. 验证部署

```bash
# 控制器健康（用信任 CA 的客户端，别用裸 curl —— 见 §9）
uv run python -c "import httpx;[print(p, httpx.Client(verify='./.ca/ca-cert.pem').get('https://localhost:8000'+p).json()) for p in ('/health/live','/health/ready','/health/active')]"

# 下载器是否已注册（DB executors 表）
uv run python -c "
import asyncio,asyncpg
async def m():
 c=await asyncpg.connect(host='localhost',port=5433,user='postgres',password='',database='dlw')
 [print(dict(r)) for r in await c.fetch('SELECT id,host_id,status,epoch,health_score,last_heartbeat_at FROM executors')]
 await c.close()
asyncio.run(m())"
# 期望：id=local-host-worker-1 status=healthy epoch>=1 last_heartbeat_at 很新

# minio 里已上传的对象
uv run python -c "import boto3;s=boto3.client('s3',endpoint_url='http://127.0.0.1:9000',aws_access_key_id='minioadmin',aws_secret_access_key='minioadmin');print([o['Key'] for o in s.list_objects_v2(Bucket='modelpull-dev').get('Contents',[])])"
# 或浏览器开 minio 控制台 http://127.0.0.1:9001  (minioadmin/minioadmin)
```

---

## 9. 故障排查（实测要点）

| 现象 | 原因 / 解法 |
|---|---|
| `dlw submit` 返回 **500**（`fk_download_tasks_owner_user_id_users`） | 用了 **system-admin 服务 token**（映射 `user_id=0`，无此用户行）。提交任务要用**租户用户 JWT**（`user_id=1`），见 §6 助手。admin token 只用于管理面。 |
| CLI/SDK **TLS 校验失败** | SP4 SDK 无 `--cacert`。自签 CA 时设 `SSL_CERT_FILE=<repo>/.ca/ca-cert.pem`（httpx 默认 SSL context 会读它）。 |
| `curl https://localhost:8000/...` 返回 `HTTP 000` | Git-Bash 下 curl 对自签 CA 的兼容性问题，**不代表控制器挂了**。用 `dlw` CLI / httpx（带 `verify=./.ca/ca-cert.pem`）验证。 |
| 401 Unauthorized（一段时间后） | 租户 JWT 默认 **1 小时** TTL 过期。重新 `source .run/dlw-env.sh`。 |
| `dlw-seed --demo` 造的任务一直 `downloading` 不动 | seed 直接插 `DownloadTask` 行、**没有子任务**；只有走 `create_task`（CLI/API `submit`）才会调 HF 枚举文件生成子任务。**手测请用 `dlw submit` 提交**，别依赖 seed 的那条 raw 任务。 |
| 下载器 `getaddrinfo failed` / poll 401 反复 | 控制器没起 HTTPS、或 server 证书 hostname 与连接地址不符。确保用 §5 的 uvicorn SSL 参数、`DLW_CONTROLLER_HOSTNAME=localhost`、下载器连 `https://localhost:8000`。 |
| 任务 `failed` | `dlw show <id>` / `tail .run/logs/executor.log` 看 `error_message`（常见：HF 仓库/revision 不存在、minio 桶未建、AWS 凭证未设）。 |

---

## 10. 停止与清理

```bash
# 关掉三个后台进程（uvicorn / dlw-executor / minio）
pkill -f "uvicorn dlw.main:create_app"; pkill -f dlw-executor; pkill -f "minio server"
#   Windows PowerShell:
#   Get-Process minio,python,uv -EA SilentlyContinue | Where-Object {$_.Path -like '*modelpull*' -or $_.ProcessName -eq 'minio'} | Stop-Process -Force

# 运行态/密钥目录（不应入库；项目 .gitignore 已忽略）
#   .run/  (日志 + minio 数据)   .ca/  (dev CA, 含私钥)   .executor-certs/  .tools/
```

---

## 11. 本地 dev vs 生产

| 方面 | 本文（本地 dev） | 生产 |
|---|---|---|
| 鉴权 | `DLW_AUTH_DEV_MODE=true` + 静态租户 JWT / admin token | 真实 OIDC（`docs/operator/oidc-setup.md`），`DLW_SYSTEM_JWT_SECRET` 改强密钥 |
| TLS/CA | 自签 dev CA（`./.ca`） | 受信 CA / 内部 PKI，证书轮换 |
| 对象存储 | 本地 minio（S3 兼容替身） | 真实 AWS S3 / 阿里云 OBS（改 `StorageBackend` 配置即可，代码不变） |
| 数据库 | 本地 PG trust/空密码 | 托管 PG + 凭证 + 备份 |
| 部署形态 | 单机 nohup 进程 | Helm（`deploy/helm`）+ active/standby 控制器 + 多 executor |

---

## 12. 延伸阅读

- 软件架构 / 特性 / 交互流程 / 独特设计（含 mermaid 图）：[README](../README.md)
- 逐步可复现运维 runbook：[`docs/operator/local-deployment.md`](./operator/local-deployment.md)
- CLI / SDK 详解：[`docs/operator/cli-sdk.md`](./operator/cli-sdk.md)
- 多源调度：[`docs/operator/multi-source.md`](./operator/multi-source.md) ｜ 多租户：[`docs/operator/multi-tenancy.md`](./operator/multi-tenancy.md)
- 增量下载 + 全局去重：[`docs/operator/incremental-download.md`](./operator/incremental-download.md)
- 接入第一个 executor：[`docs/operator/onboard-first-executor.md`](./operator/onboard-first-executor.md) ｜ executor 运维：[`docs/operator/executor-runbook.md`](./operator/executor-runbook.md)
- 设计文档（权威设计，历史追溯）：[`docs/v2.0/00-INDEX.md`](./v2.0/00-INDEX.md)
