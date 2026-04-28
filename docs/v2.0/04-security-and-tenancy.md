# 04 — 安全 / 多租户 / 配额 / 合规

> 角色：合并安全 review + 多租户/配额/合规盲区。
> 取代：v1.5 §1.4 安全问题修复，散落在各文的安全提示。

---

## 0. 从历史文档迁移指引

| 旧位置 | v2.0 位置 |
|--------|----------|
| `design_document_review_and_e2e.md` §1.4 安全问题修复 | 本文 §3 §4 §6 |
| `design_document.md` §13 API 接口（含 token 字段） | 本文 §3 凭证；接口本身见 02 |
| 各文件中的 `tenant_id` 提示 | 本文 §1 |
| 配额（v1.x 仅 `download_bytes_limit` 单任务级） | 本文 §7 |

---

## 1. 租户与身份模型（解决 G1）

### 1.1 三级模型

```
Tenant（组织）
  └─ Project（团队/项目）
       └─ User（用户）
```

- **Tenant**：最高隔离单元。tenant 之间不可跨访问。配额、审计、计费都按 tenant 聚合
- **Project**：tenant 内的逻辑分组，通常对应团队。一个 project 可绑定默认 storage
- **User**：OIDC 主体，归属唯一 tenant，可参与 0..N 个 project

### 1.2 身份提供方（OIDC）

- 推荐：Keycloak / Auth0 / Okta / 飞书 / 钉钉（社交登录）
- 必需 claims：`sub`、`email`、`groups`（用于 RBAC mapping）
- Token 类型：access_token（JWT，TTL=1h）+ refresh_token（TTL=24h）
- Library：`Authlib` (server) + `oidc-client-ts` (browser)

### 1.3 用户首次登录流程

```
1. browser → /login → 重定向到 OIDC IdP
2. IdP → callback /auth/callback?code=...
3. controller 用 code 换 token
4. 解析 sub/email/groups
5. 查 users 表：
   - 已存在：返回现有 user
   - 不存在：根据 email 域 / groups 决定 tenant 归属（配置规则）
     - 不能自动决定 → tenant_admin 审批工单
6. 签发系统 JWT（含 tenant_id, user_id, role），返回给 browser
```

### 1.4 RBAC 模型（casbin-python）

| Role | 权限 |
|------|------|
| `system_admin` | 所有 tenant 的所有操作 |
| `tenant_admin` | 本 tenant 内所有操作 |
| `tenant_operator` | 本 tenant：创建/取消任务、查看节点、发布 storage |
| `tenant_viewer` | 本 tenant：只读 |
| `project_owner` | 本 project：所有 tenant_operator 权限 limit 到 project |
| `project_member` | 本 project：创建/管理自己创建的任务 |
| `audit_reader` | 跨 tenant 的审计日志读（合规角色） |

```yaml
# casbin policy
p, role:tenant_operator, /api/tasks/*, GET|POST|DELETE, tenant_match
p, role:tenant_viewer, /api/tasks/*, GET, tenant_match
p, role:project_member, /api/tasks/*, GET|POST, project_match_or_owned
g, alice@team.com, role:project_owner, project:42
```

`tenant_match` 函数：

```python
def tenant_match(req_tenant, user_tenant):
    return req_tenant == user_tenant
```

🔒 **不变量**：所有 query 默认按 `WHERE tenant_id = current_user.tenant_id` 过滤。CI 检查所有业务 query 是否带 `tenant_id`（pg_query_filter lint）。

---

## 2. 认证与鉴权

### 2.1 用户侧（UI ↔ Controller）

详见 02 §1。要点：

- OIDC + JWT bearer
- CSRF：SameSite=Strict cookie + X-CSRF-Token 双提交（FastAPI `fastapi-csrf-protect`）
- WS：JWT 子协议握手 + Origin 白名单（详见 §4.4）

### 2.2 Executor 侧（解决 SEC-01）

#### 2.2.1 Enrollment（注册）

- Controller 启动时生成 `enrollment_secret`（256-bit），通过运维带外通道传给 executor host
- Executor 启动时用 enrollment_secret 通过 `/api/executors/register` 申请注册

```http
POST /api/executors/register
Content-Type: application/json
X-Enrollment-Token: <enrollment_secret>     # 256-bit hex
X-Enrollment-Nonce: <128-bit random>

{
  "host_id": "host-12.local",
  "executor_id_proposal": "host-12.local-worker-1",
  "tenant_id": null,                          // 可选，系统级共享 executor
  "capabilities": {"nic_speed_gbps": 10, "regions": ["cn-north-1"]},
  "client_csr": "<X.509 CSR PEM>"             // executor 自生成密钥对
}

200 OK
{
  "executor_id": "host-12.local-worker-1",
  "epoch": 14,
  "client_cert": "<X.509 cert signed by controller CA>",  // TTL = 24h
  "ca_chain": ["<ca cert>"],
  "executor_jwt": "<JWT, TTL=1h>",
  "jwt_signing_alg": "EdDSA",                              // Ed25519 签名
  "next_renew_in_seconds": 3300                            // 提前 5min 续签
}
```

#### 2.2.2 mTLS

- Controller 启动时生成自签 CA（`cryptography.hazmat.x509`）
- 注册成功的 executor 拿到由该 CA 签发的客户端证书（SVID 风格，CN=executor_id）
- Controller 端 nginx / FastAPI middleware 校验客户端证书指纹与 `executors.cert_fingerprint` 一致
- 证书 TTL=24h，到期前自动续签

#### 2.2.3 Executor JWT

- 续签 endpoint：`POST /api/executors/{eid}/renew`
- 用当前 mTLS 证书 + 当前 JWT 申请新 JWT
- claim：`{sub: executor_id, epoch: 14, tenant_id: null, exp: ..., scope: ['heartbeat', 'task.complete']}`

#### 2.2.4 心跳 HMAC（解决 SEC-04）

详见 02 §4.1。每次心跳 body 用 `enrollment_secret` 派生的 key 做 HMAC-SHA256，nonce 防重放。

### 2.3 Controller ↔ Standby

- 同一 cluster 内的 PG streaming replication 走 TLS
- standby 提升为 active 时（详见 05 §6），需要 systemd-creds 解密 standby 证书

### 2.4 节点间无横向通信（不变量 4）

Executor 之间不通信。Controller 也不主动反向连 Executor。

---

## 3. 凭证管理

### 3.1 HF Token：Reverse Proxy（解决 SEC-02）

🔒 **不变量 2**：HF Token 永不离开 Controller。

**实现**：

```
Executor                       Controller (HF Reverse Proxy)               HF
   │                                │                                       │
   │  GET /hf-proxy/{repo}/resolve/{rev}/{filename}                          │
   │  X-Subtask-Id: S                                                        │
   │  X-Assignment-Token: T                                                  │
   │  Range: bytes=0-1073741823                                              │
   ├───────────────────────────────►│                                       │
   │                                │ Verify (S, T, executor_epoch)         │
   │                                │ Look up task → tenant → HF Token from │
   │                                │   tenants.hf_tokens (envelope-encrypted)│
   │                                │ Set Authorization: Bearer <hf_token>  │
   │                                ├──────────────────────────────────────►│
   │                                │ ◄────────── 302 to CDN ───────────────┤
   │                                │                                       │
   │                                │ Follow redirect (CDN URL is short-TTL)│
   │                                │ Stream bytes back to executor         │
   │  ◄─────────────────────────────┤                                       │
   │                                │                                       │
```

**Controller 端实现要点**：

- 用 `httpx.AsyncClient(http2=True)` 流式 proxy
- 不在 Controller 落盘，纯流式转发
- 来自 HF 的速率（429）由 Controller 全局协调（详见 03 §8）
- HF Token 用 envelope encryption 存 DB；Controller memory 中只缓存 5min decrypted

```python
@app.get("/hf-proxy/{path:path}")
async def hf_proxy(path: str, request: Request, executor=Depends(verify_executor_jwt)):
    # 1. 校验 (subtask, token, epoch)
    s_id = request.headers["X-Subtask-Id"]
    token = request.headers["X-Assignment-Token"]
    subtask = await db.fetch_subtask(s_id)
    if subtask.assignment_token != token or subtask.executor_epoch != executor.epoch:
        raise HTTPException(409, "STALE_ASSIGNMENT")

    # 2. 查 tenant 的 HF token
    hf_token = await get_decrypted_hf_token(subtask.task.tenant_id)

    # 3. Build upstream URL
    upstream_url = f"https://huggingface.co/{path}?{request.query_params}"
    upstream_headers = {
        "Authorization": f"Bearer {hf_token}",
        "Range": request.headers.get("Range", ""),
    }

    # 4. Stream proxy
    async with httpx.AsyncClient() as client:
        async with client.stream("GET", upstream_url, headers=upstream_headers,
                                  follow_redirects=True) as resp:
            return StreamingResponse(
                resp.aiter_bytes(),
                status_code=resp.status_code,
                headers={k: v for k, v in resp.headers.items() if k.lower() in ALLOWED_HEADERS},
            )
```

### 3.2 Storage 凭证：STS 临时（解决 SEC-02）

🔒 **不变量 3**：Executor 不持长期 storage 凭证。

```python
async def issue_storage_credentials(executor: Executor, subtask: FileSubTask):
    storage = get_storage_backend(subtask.task.storage_id)
    long_term_credentials = decrypt_storage_config(storage.config_encrypted)

    if storage.backend_type == 's3':
        sts_client = boto3.client('sts',
            aws_access_key_id=long_term_credentials.access_key_id,
            aws_secret_access_key=long_term_credentials.secret_access_key)

        response = sts_client.assume_role(
            RoleArn=long_term_credentials.role_arn,
            RoleSessionName=f"executor-{executor.id}-{subtask.id}",
            DurationSeconds=3600,
            Policy=json.dumps({
                "Version": "2012-10-17",
                "Statement": [{
                    "Effect": "Allow",
                    "Action": ["s3:PutObject", "s3:CreateMultipartUpload",
                               "s3:UploadPart", "s3:CompleteMultipartUpload",
                               "s3:AbortMultipartUpload"],
                    "Resource": [
                        f"arn:aws:s3:::{storage.bucket}/{path_prefix}*"
                    ],
                }],
            }),
        )

        return STSCredentials(
            access_key_id=response['Credentials']['AccessKeyId'],
            secret_access_key=response['Credentials']['SecretAccessKey'],
            session_token=response['Credentials']['SessionToken'],
            expires_at=response['Credentials']['Expiration'],
        )
```

📝 **决策**：华为云 OBS / 阿里云 OSS 用各自 STS 等价 API；MinIO 用其内置 STS；NFS / local 不需要凭证（由路径权限控制）。

### 3.3 静态加密（DB 字段）

任何包含敏感信息的 DB 列必须 envelope encryption：

```python
# 对称加密（DEK = data encryption key）每个数据有自己的 DEK
# DEK 用 KEK（key encryption key，从 KMS 取）加密
# DB 存：encrypted_data || encrypted_dek

class EncryptedField:
    @staticmethod
    def encrypt(plaintext: bytes, kms_key_id: str) -> bytes:
        # 1. 生成随机 DEK
        dek = AESGCM.generate_key(bit_length=256)
        # 2. 用 DEK 加密 data
        aesgcm = AESGCM(dek)
        nonce = os.urandom(12)
        ciphertext = aesgcm.encrypt(nonce, plaintext, None)
        # 3. 用 KMS 加密 DEK
        encrypted_dek = kms_encrypt(kms_key_id, dek)
        # 4. 拼接
        return MAGIC + encrypted_dek + nonce + ciphertext

    @staticmethod
    def decrypt(blob: bytes) -> bytes: ...
```

加密字段清单：

| 表 | 字段 | KMS Key |
|----|------|---------|
| `tenant_secrets.hf_token_encrypted` | HF Token | tenant-level KEK |
| `storage_backends.config_encrypted` | S3 AK/SK / endpoint | global KEK |
| `webhook_configs.secret_encrypted` | webhook HMAC secret | tenant-level |

📝 **决策**：默认用 AWS KMS / 华为云 KMS / 本地 Vault Transit。`pgcrypto` 不够（key 与 DB 同位置）。

### 3.4 CSI Driver / k8s Secret（解决 SEC-02）

K8s 部署时不要把 HF Token 通过环境变量传递：

```yaml
# 错误：
env:
  - name: HF_TOKEN
    value: "hf_xxx"          # docker inspect 可见！

# 正确：
env:
  - name: HF_TOKEN
    valueFrom:
      secretKeyRef:
        name: dlw-secrets
        key: hf-token
volumeMounts:
  - name: secrets-vault
    mountPath: /secrets
    readOnly: true
volumes:
  - name: secrets-vault
    csi:
      driver: secrets-store.csi.k8s.io  # 从 Vault / KMS 拉
      readOnly: true
```

---

## 4. 输入校验与防注入

### 4.1 repo_id 严格正则（防 SSRF）

```python
REPO_ID_PATTERN = re.compile(r'^[A-Za-z0-9_\-]{1,96}/[A-Za-z0-9_.\-]{1,96}$')

def validate_repo_id(rid: str):
    if not REPO_ID_PATTERN.match(rid):
        raise InvalidRepoId(rid)
    if '..' in rid or '//' in rid:
        raise InvalidRepoId(rid)
```

### 4.2 HF API 调用时域名 pin

```python
# huggingface_hub SDK 使用前 monkey-patch 或自实现
ALLOWED_HF_HOSTS = {"huggingface.co", "cdn-lfs.huggingface.co", "cdn-lfs.hf.co"}

def safe_hf_get(url):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname not in ALLOWED_HF_HOSTS:
        raise SSRFAttempt(url)
    return httpx.get(url, ...)
```

### 4.3 路径穿越（解决 SEC-07）

```python
def safe_filename(name: str, base_dir: Path) -> Path:
    target = (base_dir / name).resolve()
    if not target.is_relative_to(base_dir.resolve()):
        raise PathTraversal(name)
    if any(part.startswith('.') for part in target.parts):
        raise SuspiciousPath(name)
    return target
```

文件名白名单：HF 上的 rfilename 必须只含 `[A-Za-z0-9._\-/]`，最大长度 512。

### 4.4 UI XSS 防御（解决 SEC-03）

- Vue / React 全局禁用 `v-html` / `dangerouslySetInnerHTML`，用 lint rule 强制
- 任何来自 executor 上报的字符串（filename, error_message）→ 文本节点渲染
- HTTP 响应必带：`Content-Security-Policy: default-src 'self'; script-src 'self'; object-src 'none'`

### 4.5 WebSocket Origin 白名单（解决 SEC-08）

```python
ALLOWED_ORIGINS = {"https://ui.dlw.example.com"}

@app.websocket("/ws/v1")
async def ws(ws: WebSocket):
    origin = ws.headers.get("origin")
    if origin not in ALLOWED_ORIGINS:
        await ws.close(code=1008, reason="origin_not_allowed")
        return

    # JWT 子协议
    subprotocol = ws.headers.get("sec-websocket-protocol", "")
    jwt = parse_jwt_from_subprotocol(subprotocol)
    user = verify_jwt(jwt)
    ...
```

---

## 5. 供应链安全（解决 SEC-05）

### 5.1 强制 revision 为 sha

```python
SHA_PATTERN = re.compile(r'^[0-9a-f]{40}$')

def validate_revision(rev: str):
    if not SHA_PATTERN.match(rev):
        raise InvalidRevision(rev,
            "must be a 40-char git sha; 'main', 'master', branches not allowed")
```

UI 上输入 `main` 时自动调 `huggingface_hub.HfApi().model_info(repo_id, revision="main").sha`，把 main 解析为具体 sha 后再创建任务。**用户保留对 main 的快照即时性**。

### 5.2 全文件 SHA256 指纹库

```sql
CREATE TABLE file_fingerprints (
    repo_id        VARCHAR(256) NOT NULL,
    revision       VARCHAR(64) NOT NULL,
    filename       VARCHAR(512) NOT NULL,
    sha256         VARCHAR(64) NOT NULL,
    size           BIGINT NOT NULL,
    is_lfs         BOOLEAN NOT NULL,                -- HF 是否给出官方 sha
    first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (repo_id, revision, filename)
);
```

每次下载完成都 upsert 此表。同 repo+revision 二次下载时强制比对 sha：

- 与 fingerprint 一致 → ✅
- 不一致 → 🚨 严重告警；blacklist 该源；通知 admin

### 5.3 Pickle 默认拒绝

```python
DANGEROUS_EXTENSIONS = {".bin", ".pickle", ".pkl", ".pt", ".pth"}
EXECUTABLE_PATTERNS = [r".*\.py$", r".*\.sh$"]

def file_security_classification(filename: str) -> SecurityClass:
    ext = Path(filename).suffix.lower()
    if ext == ".safetensors":
        return SecurityClass.SAFE
    if ext in DANGEROUS_EXTENSIONS:
        return SecurityClass.PICKLE_DANGEROUS
    if any(re.match(p, filename) for p in EXECUTABLE_PATTERNS):
        return SecurityClass.CODE_EXECUTION
    if filename in ("config.json", "tokenizer_config.json", "tokenizer.json"):
        # JSON 但 trust_remote_code 字段需特别处理
        return SecurityClass.JSON_NEEDS_INSPECTION
    return SecurityClass.NEUTRAL
```

下载前根据 classification：

- `SAFE`：直接下
- `PICKLE_DANGEROUS`：默认跳过；用户显式 `--allow-pickle` 才下，UI 显眼警告
- `CODE_EXECUTION`：默认跳过；trust_remote_code 风险，需 admin 审批
- `JSON_NEEDS_INSPECTION`：下载后扫描 `auto_map` / `trust_remote_code` 字段，发现可疑标黄

### 5.4 trust_remote_code 标识

```sql
ALTER TABLE download_tasks ADD COLUMN trust_remote_code_required BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE download_tasks ADD COLUMN trust_remote_code_approved_by BIGINT REFERENCES users(id);
```

任务详情页显示：⚠️ "此模型需要 trust_remote_code，已由 user@org 审批"。

### 5.5 Sigstore 验签（v2.2 roadmap）

HF 已支持 Sigstore（部分模型）。用 `sigstore-python` 校验 `.sig`。详见 06 §9。

---

## 6. DoS 与限流（解决 SEC-06）

### 6.1 路由级限流（slowapi）

详见 02 §8.1。

### 6.2 反射放大防御

外部用户调 `/api/models/search` → controller 调 HF API 转发。
风险：用户用我们的 API 当代理打 HF。

防御：

- HF API 响应缓存 60s（`fastapi-cache2` + Redis）
- 单租户每 5min 最多 30 次独立查询
- HF 限流命中时立即降级：返回 `503 UPSTREAM_DEGRADED`

### 6.3 全局并发上限

```yaml
limits:
  max_concurrent_tasks_per_tenant: 100
  max_concurrent_subtasks_per_executor: 10
  max_global_outbound_bandwidth_gbps: 100      # 出口总带宽配额
  max_storage_writes_per_second_per_tenant: 1000  # 防 abuse
```

---

## 7. 配额与计量（解决 G2）

### 7.1 三类配额

| 配额 | 单位 | 强制时机 |
|------|------|---------|
| 月下载流量 | bytes | 任务创建 + chunk 完成时累计 |
| 存储占用 | bytes | 任务创建 + storage_object refcount 变化 |
| 并发任务数 | int | 任务创建时 |

### 7.2 计量上报链路

```
Executor (chunk 完成)
    ↓ 心跳上报 chunk_bytes_completed
Controller
    ↓ 写 usage_records (event-sourced)
    ↓ 异步聚合到 quota_snapshots（每分钟）
Quota Manager
    ↓ 任务创建时强一致检查
```

### 7.3 计量数据流（详细）

```sql
-- usage_records 是 append-only 事件源
CREATE TABLE usage_records (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     BIGINT NOT NULL,
    project_id    BIGINT,
    user_id       BIGINT,
    task_id       UUID,
    metric        VARCHAR(64) NOT NULL,
    value         BIGINT NOT NULL,
    region_pair   VARCHAR(64),
    occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_usage_tenant_metric_time ON usage_records(tenant_id, metric, occurred_at);

-- quota_snapshots 是聚合（每分钟 cron 重算）
CREATE TABLE quota_snapshots (
    tenant_id           BIGINT PRIMARY KEY REFERENCES tenants(id),
    bytes_used_month    BIGINT NOT NULL DEFAULT 0,
    storage_gb_used     BIGINT NOT NULL DEFAULT 0,
    concurrent_tasks    INT NOT NULL DEFAULT 0,
    last_recomputed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 7.4 配额耗尽行为

```yaml
# 租户级别可配置
quota_exceeded_action:
  bytes_month: hard_block       # hard_block / throttle / overage_billing
  storage_gb: throttle           # 仅警告，但不创建新任务
  concurrent_tasks: hard_block
```

- **hard_block**：返回 429 `QUOTA_EXCEEDED`，任务无法创建
- **throttle**：任务可创建但 priority 自动降到 0，速度全局限制 1/4
- **overage_billing**：超额按 chargeback 计入下月账单（需对接计费系统）

### 7.5 用量 API

```http
GET /api/quota/current
GET /api/quota/usage?from=2026-04-01&to=2026-04-30&group_by=project
GET /api/quota/forecast      # ML 预测下月用量

200 OK
{
  "tenant_id": 1,
  "bytes_used_month": 12345678901234,
  "bytes_quota_month": 50000000000000,
  "storage_gb_used": 1024,
  "storage_gb_quota": 5120,
  "concurrent_tasks": 4,
  "concurrent_quota": 10,
  "remaining_days_in_billing_period": 12,
  "forecast_month_end_usage": 38000000000000     // ML 预测
}
```

### 7.6 Chargeback 报表

每月 1 号生成 PDF/CSV：

- Per project：流量 / 存储 / 任务数
- Per region pair：跨地域流量（成本权重最高）
- Top 10 模型（按字节）
- Per user：用户级排名

---

## 8. 合规与治理（解决 G3）

### 8.1 License 白/黑名单

```sql
CREATE TABLE license_policies (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     BIGINT NOT NULL REFERENCES tenants(id),
    license       VARCHAR(64) NOT NULL,            -- 'apache-2.0' / 'mit' / 'gpl-3.0' / 'meta-llama-3'
    policy        VARCHAR(16) NOT NULL,            -- allow / deny / warn
    reason        TEXT,
    UNIQUE (tenant_id, license)
);
```

任务创建时：

```python
def check_license_policy(repo_info, tenant):
    license = repo_info.get('license') or repo_info.get('cardData', {}).get('license')
    policy = db.fetch_license_policy(tenant.id, license)
    if policy == 'deny':
        raise LicenseDenied(license)
    if policy == 'warn':
        return Warning(f"License {license} requires legal review")
```

### 8.2 Gated 模型审批工作流

部分模型在 HF 上 gated（如 Meta Llama 系列需协议）：

```sql
CREATE TABLE gated_model_approvals (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       BIGINT NOT NULL,
    repo_id         VARCHAR(256) NOT NULL,
    requested_by    BIGINT NOT NULL REFERENCES users(id),
    approved_by     BIGINT REFERENCES users(id),       -- tenant_admin
    legal_ticket_url TEXT,
    status          VARCHAR(16) NOT NULL,                -- pending / approved / denied
    requested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    approved_at     TIMESTAMPTZ,
    UNIQUE (tenant_id, repo_id)
);
```

任务流：用户提交 gated 模型任务 → 系统检测 gated → 创建 approval ticket → admin 审批 → approved 后任务才进 pending。

### 8.3 出口管制

某些模型 / 区域受出口管制（EAR / EU dual-use / 中国 GB 35114）：

```yaml
export_control_rules:
  - blocked_origin_country_codes: [<list>]
    blocked_destination_country_codes: [<list>]
    repo_pattern: "*"
  - allowed_only_for_regions: [cn-north]
    repo_pattern: "deepseek-ai/*"
```

任务创建时检查 `executor.region` 与 `repo_id` 的合规性。

### 8.4 多源合规（与 06 §1.13 联动）

📝 **决策**：跨源下载尊重 HF gated 状态——即便其他源未设 gated，gated 模型仍走 §8.2 审批流程。

### 8.5 审批工作流

```
用户提交可疑任务
    ↓
系统检测（license / gated / trust_remote_code / export_control）
    ↓ 命中
任务进入 pending_approval（不走 scheduler）
    ↓
通知 tenant_admin（webhook / email / Slack）
    ↓
admin 在 UI 审批（approve / deny + reason）
    ↓
approved → task.status = 'pending'，进 scheduler
denied   → task.status = 'failed'，error_message 含 reason
```

---

## 9. 审计日志（解决 SEC-09）

### 9.1 链式哈希（tamper-evident）

```sql
CREATE TABLE audit_log (
    id              BIGSERIAL PRIMARY KEY,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id       BIGINT,
    actor_user_id   BIGINT,
    actor_ip        INET,
    action          VARCHAR(64) NOT NULL,            -- task.create / executor.register / quota.exceed / ...
    resource_type   VARCHAR(32) NOT NULL,
    resource_id     VARCHAR(128),
    outcome         VARCHAR(16) NOT NULL,            -- success / denied / error
    payload         JSONB,                            -- 操作参数（脱敏）
    trace_id        VARCHAR(32),
    prev_hash       VARCHAR(64),                      -- 前一行的 sha256
    self_hash       VARCHAR(64) NOT NULL              -- 本行的 sha256
);

CREATE INDEX idx_audit_actor ON audit_log(actor_user_id, occurred_at);
CREATE INDEX idx_audit_resource ON audit_log(resource_type, resource_id);
CREATE INDEX idx_audit_action_time ON audit_log(action, occurred_at);

-- 启用 PostgreSQL pgaudit 扩展记录所有 DDL/写入
```

每条记录的 `self_hash`：

```
self_hash = sha256(prev_hash || occurred_at || action || resource_id ||
                   outcome || payload || actor_user_id)
```

定期 dump 到 WORM 存储（S3 Object Lock COMPLIANCE 模式）：

- 每日生成 `audit-{date}.parquet` 上传到 `s3://audit/dlw/`
- Bucket lifecycle: COMPLIANCE 模式 ≥ 365 天

### 9.2 必记录的事件

| 类别 | 事件 | 关键字段 |
|------|------|---------|
| 认证 | login, logout, token_refresh, mfa_enrolled | actor_ip |
| 授权 | role_grant, role_revoke, permission_denied | actor, resource |
| 任务 | task.create, task.cancel, task.upgrade, task.priority_change | task_id, repo_id |
| 凭证 | hf_token.set, storage.config_change, sts.issued | tenant_id |
| 合规 | gated_approval, license_override, export_control_block | repo_id |
| 配额 | quota.exceeded, quota.adjust | tenant_id |
| 系统 | controller.restart, controller.failover, db.migration | — |
| 审计本身 | audit.export, audit.search | actor |

### 9.3 日志脱敏（解决 SEC-11）

```python
SENSITIVE_PATTERNS = [
    (re.compile(r'hf_[A-Za-z0-9]{30,}'), '[HF_TOKEN]'),
    (re.compile(r'AKIA[0-9A-Z]{16}'), '[AWS_AK]'),
    (re.compile(r'AccessKeyId=\S+'), 'AccessKeyId=[REDACTED]'),
    (re.compile(r'Bearer\s+\S+'), 'Bearer [REDACTED]'),
    (re.compile(r'Authorization:\s*\S+'), 'Authorization: [REDACTED]'),
]

class RedactProcessor:
    def __call__(self, logger, name, event_dict):
        for k, v in event_dict.items():
            if isinstance(v, str):
                for pat, repl in SENSITIVE_PATTERNS:
                    v = pat.sub(repl, v)
                event_dict[k] = v
        return event_dict

structlog.configure(processors=[..., RedactProcessor(), JSONRenderer()])
```

CI 检查：用 `gitleaks` 扫描日志样本。

---

## 10. 多租户 metrics 隔离

Prometheus metrics 加 `tenant_id` label，注意基数：

```
# 高基数风险，按 tenant 而非 task 聚合
download_bytes_total{tenant_id="42", source_id="modelscope"}
task_count{tenant_id="42", status="downloading"}
```

❌ 不要：`{task_id="..."}`（UUID 基数爆炸）

详见 05 §1。

---

## 11. 与其他文档的链接

- API 协议：→ [02-protocol.md](./02-protocol.md)
- HF Token 反代时序：→ [03-distributed-correctness.md](./03-distributed-correctness.md) §2
- SLO / 告警：→ [05-operations.md](./05-operations.md)
- 多源合规细节：→ [06-platform-and-ecosystem.md](./06-platform-and-ecosystem.md) §1.13
