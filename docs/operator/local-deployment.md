# 本地部署 Runbook（控制器 + 下载器 + 对象存储）

> 可复现的逐步运维 runbook —— 在一台机器上把整套系统拉起来，用 CLI/SDK 真实下载
> HF 模型。命令均经实测。面向 **本地 / dev**；生产差异见末节。
> 概念与"为什么这样设计"见 [README · 软件架构 / 独特软件设计](../../README.md)；
> 上手叙述见 [`docs/getting-started.md`](../getting-started.md)。

---

## 0. 数据流（先有概念再动手）

```
HuggingFace ──HTTP──▶ 控制器(反向代理,注入并隐藏 HF token)
                          │
                          ▼ 下载器经控制器代理流式拉字节
                      下载器 ──S3 multipart(+sha256 tee)──▶ 对象存储(本地 minio)
                          ▲
                          └─ mTLS 注册 + executor-JWT + 心跳HMAC ─▶ 控制器/PG
```

- **下载用 HTTP**（源是 HF，经控制器代理）；**存储用 S3 协议**（写入对象存储）。
- 本地用 **minio** 在 `:9000` 顶替云 S3 —— S3 协议照用，端点是本地 minio。
- 存储后端可插拔（`StorageBackend.backend_type`），上生产只改 `endpoint_url`。

---

## 1. 前置

| 依赖 | 要求 |
|---|---|
| Python 3.12 + `uv` | `uv sync` 装好依赖 |
| PostgreSQL | `localhost:5433`，`postgres` 用户、空密码/trust，库名 `dlw` |
| minio 二进制 | [下载](https://min.io/download)，放进 PATH 或项目 `.tools/`（`.tools/` 已 gitignore） |
| 出网 | 能访问 `huggingface.co`（控制器反向代理需真实拉 HF） |

```bash
git clone https://github.com/l17728/modelpull && cd modelpull && uv sync
mkdir -p .tools .run/minio-data .run/logs
# linux: curl -sL -o .tools/minio https://dl.min.io/server/minio/release/linux-amd64/minio && chmod +x .tools/minio
# win:   curl -sL -o .tools/minio.exe https://dl.min.io/server/minio/release/windows-amd64/minio.exe
```

---

## 2. 数据库

```bash
psql -h localhost -p 5433 -U postgres -d postgres -c "CREATE DATABASE dlw"   # 已存在忽略报错
uv run alembic upgrade head        # alembic env.py 用 get_settings().db_url（默认即 localhost:5433/dlw）
```

校验：`uv run python -c "import asyncio,asyncpg; ..."` 应能列出 ~18 张表（含
`download_tasks/file_subtasks/executors/storage_backends/storage_objects/...`）。

---

## 3. minio（本地 S3 后端）

```bash
MINIO_ROOT_USER=minioadmin MINIO_ROOT_PASSWORD=minioadmin \
  nohup .tools/minio server .run/minio-data \
  --address 127.0.0.1:9000 --console-address 127.0.0.1:9001 \
  > .run/logs/minio.log 2>&1 &

uv run python -c "import boto3;boto3.client('s3',endpoint_url='http://127.0.0.1:9000',\
aws_access_key_id='minioadmin',aws_secret_access_key='minioadmin').create_bucket(Bucket='modelpull-dev')"
```

控制台：`http://127.0.0.1:9001`（`minioadmin`/`minioadmin`）。

---

## 4. dev CA（HTTPS + mTLS 基座）

> 控制器的 lifespan 会自举 `./.ca`，但 **uvicorn 的 SSL 在 lifespan 之前就要读证书文件**，
> 所以先手动预生成（lifespan 之后会幂等复用）。server 证书的 hostname 必须与下载器
> 连接用的主机名一致 —— 用 `localhost`。

```bash
uv run python -c "
from pathlib import Path
from dlw.auth.ca import bootstrap_ca, ensure_server_cert
from dlw.auth.jwt_signing import bootstrap_keypair
d=Path('./.ca'); d.mkdir(mode=0o700, parents=True, exist_ok=True)
ca=bootstrap_ca(d); ensure_server_cert(ca,d,hostname='localhost'); bootstrap_keypair(d)
(d/'enrollment.token').write_text('local-enroll-token')
print(sorted(p.name for p in d.iterdir()))"
# → ca-cert.pem ca-key.pem enrollment.token jwt-signing.pem server-cert.pem server-key.pem
```

`.ca/`（含私钥）、`.executor-certs/`、`.run/`、`.tools/` 均已在 `.gitignore`，不入库。

---

## 5. seed 数据 + 修正存储后端指向 minio

```bash
uv run dlw-seed --demo          # 建 tenant(1)/project(1)/user(1,tenant_admin)/storage(1) + 1 个 demo 任务
```

> ⚠️ `dlw-seed` 用 `ON CONFLICT (id) DO NOTHING`。若 `storage_backends(id=1)` 已存在
> （早前跑测试/手动插入留下的），它的 config **不会**被覆盖（常见为垃圾值 `'x'`）。
> 必须把它显式指向本地 minio：

```bash
uv run python -c "
import asyncio,asyncpg,json
cfg=json.dumps({'bucket':'modelpull-dev','region':'us-east-1',
                'endpoint_url':'http://127.0.0.1:9000','key_prefix':'phase1/'})
async def m():
 c=await asyncpg.connect(host='localhost',port=5433,user='postgres',password='',database='dlw')
 await c.execute(\"UPDATE storage_backends SET config_encrypted=\$1::bytea,\
region='us-east-1',backend_type='s3' WHERE id=1\", cfg.encode())
 print(await c.fetchval(\"SELECT convert_from(config_encrypted,'utf8') FROM storage_backends WHERE id=1\"))
 await c.close()
asyncio.run(m())"
```

> ⚠️ `dlw-seed --demo` 直接插的那条 `DownloadTask` **没有子任务**（不会动）。
> 真实手测请用 `dlw submit`（走 `create_task` → 调 HF 枚举文件 → 生成子任务）。
> 可选：清掉 seed 的 raw 任务保持干净：
> `DELETE FROM file_subtasks; DELETE FROM download_tasks;`

---

## 6. 启动控制器（uvicorn HTTPS + 可选 mTLS）

```bash
DLW_AUTH_DEV_MODE=true \
DLW_SYSTEM_ADMIN_TOKEN=local-admin-token \
DLW_ENROLLMENT_TOKEN=local-enroll-token \
DLW_CONTROLLER_HOSTNAME=localhost \
  nohup uv run uvicorn dlw.main:create_app --factory --host 127.0.0.1 --port 8000 \
  --http httptools \
  --ssl-certfile ./.ca/server-cert.pem --ssl-keyfile ./.ca/server-key.pem \
  --ssl-ca-certs ./.ca/ca-cert.pem --ssl-cert-reqs 1 \
  > .run/logs/controller.log 2>&1 &
```

要点：
- `--factory`：`dlw.main:create_app` 工厂，跑真实 lifespan（leader 选主→active、casbin、源注册）。
- `--http httptools`：mTLS 对端证书需 httptools 后端 + `uvicorn_tls_patch` 注入 transport。
- `--ssl-cert-reqs 1` = `CERT_OPTIONAL`：注册无客户端证书；poll/heartbeat 带证书。
- `DLW_AUTH_DEV_MODE=true`：跳过 fail-closed 守卫（生产用真实 OIDC + 强 `DLW_SYSTEM_JWT_SECRET`）。

健康检查（用信任 CA 的客户端；**别用裸 curl**）：

```bash
uv run python -c "import httpx;[print(p, httpx.Client(verify='./.ca/ca-cert.pem').\
get('https://localhost:8000'+p).json()) for p in ('/health/live','/health/ready','/health/active')]"
# 期望: live healthy / ready db ok / active controller_state=active
```

---

## 7. 启动下载器（自动 mTLS 注册）

ExecutorSettings 用 **`DLW_EXECUTOR_`** 前缀（与控制器的 `DLW_` 不同）。

```bash
mkdir -p .executor-certs
DLW_EXECUTOR_ID=local-host-worker-1 \
DLW_EXECUTOR_BEARER_TOKEN=unused \
DLW_EXECUTOR_CONTROLLER_URL=https://localhost:8000 \
DLW_EXECUTOR_ENROLLMENT_TOKEN=local-enroll-token \
DLW_EXECUTOR_EXECUTOR_CERT_DIR=./.executor-certs \
DLW_EXECUTOR_EXECUTOR_CA_BUNDLE=./.ca/ca-cert.pem \
DLW_EXECUTOR_S3_ENDPOINT_URL=http://127.0.0.1:9000 \
DLW_EXECUTOR_S3_REGION=us-east-1 \
AWS_ACCESS_KEY_ID=minioadmin AWS_SECRET_ACCESS_KEY=minioadmin AWS_DEFAULT_REGION=us-east-1 \
  nohup uv run dlw-executor --log-level INFO > .run/logs/executor.log 2>&1 &
```

> `DLW_EXECUTOR_BEARER_TOKEN` 是历史必填字段（min_length=1），客户端鉴权已是 mTLS，
> 填任意非空即可。`DLW_EXECUTOR_EXECUTOR_CA_BUNDLE` 让首个 register 调用就能校验
> 控制器自签证书（运维 OOB 随 enrollment token 一起下发，符合真实流程）。

**验证已注册**（DB `executors` 表）：

```bash
uv run python -c "
import asyncio,asyncpg
async def m():
 c=await asyncpg.connect(host='localhost',port=5433,user='postgres',password='',database='dlw')
 [print(dict(r)) for r in await c.fetch('SELECT id,host_id,status,epoch,health_score,last_heartbeat_at FROM executors')]
 await c.close()
asyncio.run(m())"
# 期望: id=local-host-worker-1 status=healthy epoch>=1 last_heartbeat_at 很新
```

`tail -f .run/logs/executor.log` 应见 `register 201` → 持续 `heartbeat/poll 200`。

---

## 8. 用 CLI/SDK 真实下载

```bash
cat > .run/dlw-env.sh <<'EOF'
export DLW_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export SSL_CERT_FILE="$DLW_REPO/.ca/ca-cert.pem"
export DLW_SERVER="https://localhost:8000"
export DLW_TOKEN="$(uv run python -c "from dlw.auth.principal import issue_system_jwt; print(issue_system_jwt(secret='dev-system-jwt-change-me', user_id=1, tenant_id=1, role='tenant_admin', project_ids=[]))" 2>/dev/null)"
EOF
source .run/dlw-env.sh

uv run dlw submit sentence-transformers/all-MiniLM-L6-v2 \
  -r c9745ed1d9f207416be6d2e6f8de32d1f16199bf -s 1
uv run dlw watch <task-id>          # 轮询到 succeeded

# 看 minio 里的对象
uv run python -c "import boto3;s=boto3.client('s3',endpoint_url='http://127.0.0.1:9000',\
aws_access_key_id='minioadmin',aws_secret_access_key='minioadmin');\
print([o['Key'] for o in s.list_objects_v2(Bucket='modelpull-dev').get('Contents',[])])"
# → phase1/sentence-transformers/all-MiniLM-L6-v2/<rev>/model.safetensors ...
```

---

## 9. 排错矩阵（实测）

| 现象 | 根因 / 解法 |
|---|---|
| `dlw submit` → 500 `fk_download_tasks_owner_user_id_users` | 用了 system-admin token（`user_id=0` 无此用户）。提交用**租户用户 JWT**（`user_id=1`，见 §8 助手）。 |
| CLI/SDK TLS 校验失败 | SDK 无 `--cacert`；设 `SSL_CERT_FILE=<repo>/.ca/ca-cert.pem`。 |
| `curl https://localhost:8000` → `HTTP 000` | Git-Bash curl 对自签 CA 的兼容问题，非控制器故障。用 `dlw`/httpx(`verify=`)。 |
| 401（一段时间后） | 租户 JWT TTL 1h 过期，重新 `source .run/dlw-env.sh`。 |
| seed 的 demo 任务一直 `downloading` | 那条是 raw 行无子任务；用 `dlw submit` 提交真实任务。 |
| 下载器反复 `getaddrinfo failed` / poll 401 | 控制器没起 HTTPS / server 证书 hostname 不匹配。确认 §6 的 `--ssl-*` + `DLW_CONTROLLER_HOSTNAME=localhost` + 下载器连 `https://localhost:8000`。 |
| 任务 `failed` | `dlw show <id>` 看 `error_message`；常见 HF 仓库/revision 不存在、minio 桶未建、AWS 凭证未设。 |
| `storage_backends(1)` config = `'x'` | seed 的 ON CONFLICT 没覆盖既有行，执行 §5 的 UPDATE 显式指向 minio。 |

---

## 10. 停止 / 清理

```bash
pkill -f "uvicorn dlw.main:create_app"; pkill -f dlw-executor; pkill -f "minio server"
# Windows: Get-Process minio,python,uv -EA SilentlyContinue |
#   Where-Object {$_.Path -like '*modelpull*' -or $_.ProcessName -eq 'minio'} | Stop-Process -Force

# 彻底重来（dev DB 可丢）：
#   DROP/CREATE DATABASE dlw → alembic upgrade head；rm -rf .run/minio-data/* .ca .executor-certs
```

---

## 11. 本地 dev vs 生产

| 方面 | 本 runbook | 生产 |
|---|---|---|
| 鉴权 | `DLW_AUTH_DEV_MODE=true` + 静态 JWT | 真实 OIDC（`oidc-setup.md`）+ 强 `DLW_SYSTEM_JWT_SECRET` |
| CA/TLS | 自签 dev CA | 受信 CA / 内部 PKI + 轮换（`rotate-executor-mtls.sh`） |
| 存储 | 本地 minio | 真实 S3/OBS（仅改 `StorageBackend` 配置） |
| DB | 本机 PG trust | 托管 PG + 凭证 + 备份（`verify-backup.sh`） |
| 形态 | 单机 nohup | Helm（`deploy/helm`）+ active/standby + 多 executor |

相关：[`onboard-first-executor.md`](./onboard-first-executor.md) ·
[`executor-runbook.md`](./executor-runbook.md) ·
[`oidc-setup.md`](./oidc-setup.md) ·
[`cli-sdk.md`](./cli-sdk.md) ·
[`multi-source.md`](./multi-source.md) ·
[`incremental-download.md`](./incremental-download.md)
