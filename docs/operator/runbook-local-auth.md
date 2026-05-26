# Runbook — 本地认证常见故障

> 适用：用户/管理员密码遗忘、bootstrap 失败、用户被锁、多个 admin 互相重置、迁移
> 到本地认证的步骤。
> 对应特性：`/api/v1/auth/local/*` （详见 [`docs/operator/oidc-setup.md` § 附 local
> auth 备选](./oidc-setup.md)）。

---

## 1. admin 密码遗忘 / 丢失

### 场景 A：还有另一个 `system_admin` 可登录

让另一个 admin 重置：

**前端**：登录 → Settings → 用户管理 → 找到目标用户 → 「重置密码」按钮

**或 curl**：

```bash
ADMIN_TOKEN=$(curl -s -X POST http://127.0.0.1:8001/api/v1/auth/local/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"other_admin","password":"..."}' | jq -r .access_token)

# 查目标用户 id
curl -sS -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8001/api/v1/auth/local/users | jq '.[] | select(.username=="admin")'

# 重置
curl -X POST http://127.0.0.1:8001/api/v1/auth/local/users/<user_id>/reset \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"new_password":"NewStrongPass1234"}'
```

被重置的用户下次登录会被强制改密（`must_change_password=true`）。

### 场景 B：**唯一的** admin 密码丢失（最严重）

只能直连 PG 手动改密码 hash。这是 destructive 操作 — 操作前**先备份**。

```bash
# 1. 生成新密码的 argon2 hash
python -c "
from argon2 import PasswordHasher
print(PasswordHasher().hash('NewStrongPass1234'))
"
# 输出形如: $argon2id$v=19$m=65536,t=3,p=4$...

# 2. 直接 UPDATE
psql -h localhost -p 5433 -U postgres -d dlw -c "
UPDATE local_credentials
SET password_hash = '<上面的 hash>',
    must_change_password = true
WHERE username = 'admin';
"

# 3. 验证可登录
curl -X POST http://127.0.0.1:8001/api/v1/auth/local/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"NewStrongPass1234"}'
# 应返回 access_token 且 must_change_password=true
```

**事后必做**：登录后通过 Settings → 修改密码 改成长期密码（让用户而非操作员
持有最终密码），并写入审计：

```sql
INSERT INTO audit_log (action, resource_type, resource_id, outcome, payload, self_hash)
VALUES ('auth.password.emergency_reset', 'user', '<user_id>', 'success',
        '{"reason":"forgotten_admin_password","operator":"<your_name>"}',
        '');
```

---

## 2. Bootstrap 失败（首次启动）

### 症状：controller 启动报 `UniqueViolationError: pk_users id=1 already exists`

**根因**：DB 里已经有 `users` 行（例如 dev 模式的 `oidc_subject='dev'` 用户），但 PG
序列 `users_id_seq` 没被推进。bootstrap_admin 创建 User 时序列返回 1，跟既存
行冲突。

**修**（dev DB；prod 一般 fresh，不会触发）：

```bash
psql -h localhost -p 5433 -U postgres -d dlw -c "
SELECT setval('users_id_seq', (SELECT MAX(id) FROM users));
"
```

然后重启 controller。

### 症状：启动日志没有 `bootstrapped local admin user 'admin'`

**根因**：`DLW_ADMIN_INITIAL_PASSWORD` 没设。bootstrap 只在该 env var 非空时执行。

**修**：

```bash
DLW_ADMIN_USERNAME=admin \
DLW_ADMIN_INITIAL_PASSWORD=Bootstrap-Strong-Pass-1234 \
uv run uvicorn dlw.main:app --port 8001
```

**安全提醒**：bootstrap 完成后**立即**改成长期密码 + 把 env var 从 systemd /
.env 里删除，避免明文密码持久化。

### 症状：bootstrap 跑了但 admin 已存在 — 不会改密

这是**预期行为**（idempotent）。`bootstrap_admin()` 检查到既存 admin 就跳过，不会
覆盖密码。要重置密码，走 §1 流程。

---

## 3. 用户太多被锁 / 误删

### 删错用户

```bash
# 直接重建
curl -X POST http://127.0.0.1:8001/api/v1/auth/local/users \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"username":"recreated","password":"...","tenant_id":1,"role":"tenant_operator"}'
```

**注意**：新用户的 `user_id` 跟原来的不同。如果原 user 还有 task / audit 历史，
会变成"孤儿"记录（FK 还指向旧的 user row — local_credentials 行被删了但 `users`
行还在）。审计/历史**不会丢**，只是无法通过 username 关联。

要彻底删（包括 `users` 行）需要手动 DDL，**强烈不推荐**（会破坏审计可追溯性）。

### 用户 must_change_password 状态被卡

```sql
UPDATE local_credentials SET must_change_password = false WHERE user_id = <id>;
```

---

## 4. 切换 OIDC ↔ Local Auth

### Local → OIDC

按 [`oidc-setup.md`](./oidc-setup.md) 主流程配置 IdP；**保留 local auth admin** 作
为 break-glass 通道（OIDC IdP 挂了仍能登录改配置）。

### OIDC → Local

```bash
# 1. 启动时加 bootstrap admin
DLW_ADMIN_INITIAL_PASSWORD=... uv run uvicorn dlw.main:app

# 2. 通知用户：登录页底部「使用 OIDC 登录」按钮仍在，但首次需用本地凭证创建
# 3. system_admin 在 Settings → 用户管理 里给每个用户建本地凭证
# 4. 旧 OIDC 用户的 user_id / tenant_id 在 users 表里保留，audit / task / quota
#    历史全部继承（local_credentials 表通过 user_id 关联到 users 表）
```

---

## 5. Startup guard 拒绝启动

```
RuntimeError: insecure config in non-dev mode
```

**根因 1**：`DLW_SYSTEM_JWT_SECRET` 还是默认值 `dev-system-jwt-change-me`

**修**：

```bash
DLW_SYSTEM_JWT_SECRET=$(openssl rand -hex 32)
```

**根因 2**（已废弃）：之前要求必须配 OIDC issuer — Phase 4 后**已放宽**，local
auth 也是合法替代，不再要求 issuer。如果仍报这个错，说明 controller 在跑老
代码（参见 `feedback_dev_server_port_proxy` memory）。

---

## 6. 审计 / 合规取证

所有 local auth 写操作进审计：

```sql
SELECT created_at, action, resource_id, outcome, payload, actor_user_id
FROM audit_log
WHERE action LIKE 'auth.%' OR action LIKE 'tenant.quota.%'
ORDER BY created_at DESC LIMIT 50;
```

关键 action：
- `auth.local.login.success` / `.failure`
- `auth.local.user.create`
- `auth.local.password.change` / `.reset`
- `tenant.quota.update` — payload 含 `{field: {before, after}}`

`self_hash` 字段是当前行的 SHA-256；下个 phase 会引入 `prev_hash` 链式校验。

---

## 7. 关联文档

- [`oidc-setup.md`](./oidc-setup.md) — 主流程 + local auth 备选章节
- [`multi-tenancy.md`](./multi-tenancy.md) — tenant_id / role / casbin 关系
- [`MANUAL.md`](../../MANUAL.md) § AI 助手 — `dlw_create_local_user` / `dlw_reset_local_password` AI 工具
