# OIDC IdP 配置指南

> 用途：解决 FEAS-04 报告的 day-zero 问题 —— 第一个 tenant_admin / system_admin 怎么登录？OIDC client_id 哪里来？
> 适用：v2.0 GA Phase 3 起；初次部署或新 tenant 接入时使用。
> 估时：30-60 min（取决于 IdP 类型）。

> **备选方案（2026-05 起）**：如果你的部署**没有可用的 OIDC IdP**（离线 / 气隙
> 网络 / POC 验证），可以改用**本地用户名密码认证**，跳过本文档 — 见末尾的
> [附：local auth 备选](#附local-auth-备选不依赖-oidc) 章节。OIDC 仍是企业部署
> 的推荐方案；local auth 适合受限网络场景。

---

## 0. 决策：选 IdP

| IdP | 适用 | 复杂度 |
|-----|------|------|
| **Keycloak**（自托管） | 私有部署 / 内网 / 数据合规 | 中 |
| **Auth0** | SaaS 客户 / 无运维成本偏好 | 低 |
| **Okta** | 大企业 / 已有 Okta 投资 | 中 |
| **Azure AD / Entra ID** | 微软生态 / Office 365 客户 | 中 |
| **Google Workspace** | Google 生态 / 教育 | 低 |
| **飞书 / 钉钉**（社交登录） | 中国境内 SaaS | 中 |

modelpull 通过标准 OIDC PKCE flow 集成，与 IdP 无关。本文以 **Keycloak** 和 **Auth0** 为例。

---

## 1. Keycloak（自托管）

### 1.1 假设

- Keycloak ≥ 22.x 已部署
- 有 admin 账号
- DLW controller 部署到 `https://api.dlw.example.com`
- DLW UI 部署到 `https://dlw.example.com`

### 1.2 创建 Realm

```
Keycloak admin console → Master realm → Add realm
  Name: dlw
  Enabled: ON
  → Save
```

### 1.3 创建 Client

```
Realm dlw → Clients → Create client
  Client ID: modelpull
  Client type: OpenID Connect
  Authentication flow:
    [✓] Standard flow (Authorization Code)
    [✓] OAuth 2.0 Device Authorization Grant  ← CLI 用
  Valid redirect URIs:
    - https://dlw.example.com/auth/callback
    - http://localhost:8000/auth/callback     # 开发用
    - http://localhost:18555/                  # CLI device code callback
  Web origins:
    - https://dlw.example.com
  → Save

→ Credentials tab → 记录 Client Secret（用于 dlw admin bootstrap）
```

### 1.4 创建 Group + Role

```
Realm dlw → Groups → Add Group: dlw-admins
Realm dlw → Roles → Add Role:
  - tenant_admin
  - tenant_operator
  - tenant_viewer
  - system_admin

Groups → dlw-admins → Role mappings → assign system_admin
```

### 1.5 创建第一个 admin 用户

```
Realm dlw → Users → Add user
  Username: alice
  Email: alice@example.com
  Email verified: ON
  Groups: dlw-admins
  → Save

→ Credentials tab → Set password (temporary)
```

### 1.6 配置 modelpull controller

把 OIDC info 注入 controller helm values：

```yaml
# values.production.yaml
security:
  oidc:
    issuer: https://keycloak.example.com/realms/dlw
    client_id: modelpull
    client_secret: ${OIDC_CLIENT_SECRET}     # 从 Vault / K8s Secret
    redirect_uri: https://dlw.example.com/auth/callback

    # group → role mapping
    role_mapping:
      claim: groups          # OIDC claim 字段
      mapping:
        dlw-admins: system_admin
        dlw-team-a: tenant_admin
```

---

## 2. Auth0（SaaS）

### 2.1 创建 Application

```
Auth0 dashboard → Applications → Create Application
  Name: modelpull
  Type: Single Page Application (SPA)
  → Create

→ Settings tab:
  Allowed Callback URLs:
    https://dlw.example.com/auth/callback,
    http://localhost:8000/auth/callback,
    http://localhost:18555/
  Allowed Logout URLs:
    https://dlw.example.com/
  Allowed Web Origins:
    https://dlw.example.com
  → Save Changes

→ 记录 Domain + Client ID
```

### 2.2 创建 API（用于 access_token audience）

```
Auth0 dashboard → APIs → Create API
  Name: modelpull-api
  Identifier: https://api.dlw.example.com
  Signing Algorithm: RS256
  → Create

→ Settings → Add scopes:
  - read:tasks
  - write:tasks
  - admin:all
```

### 2.3 创建 Role + 用户

```
Auth0 → User Management → Roles → Create Role:
  - tenant_admin
  - tenant_operator
  - tenant_viewer
  - system_admin

→ Users → Create User:
  alice@example.com → Roles tab → assign system_admin
```

### 2.4 配置 modelpull controller

```yaml
security:
  oidc:
    issuer: https://your-tenant.auth0.com/
    client_id: <auth0 spa client id>
    client_secret: ""    # SPA 不需要 client secret（PKCE）
    audience: https://api.dlw.example.com   # 必填，对应 Auth0 API
    redirect_uri: https://dlw.example.com/auth/callback
    role_mapping:
      claim: "https://dlw.example.com/roles"   # Auth0 通常用 namespace 化 claim
      mapping:
        system_admin: system_admin
```

需要在 Auth0 Action 里把 roles 加到 token claims：

```javascript
// Auth0 → Actions → Library → Build Custom
exports.onExecutePostLogin = async (event, api) => {
  const namespace = 'https://dlw.example.com';
  if (event.authorization) {
    api.idToken.setCustomClaim(`${namespace}/roles`, event.authorization.roles);
    api.accessToken.setCustomClaim(`${namespace}/roles`, event.authorization.roles);
  }
};
```

---

## 3. 第一次 system_admin 登录 (`dlw admin bootstrap`)

无论用哪种 IdP，第一个 system_admin 进入 modelpull 都需要 bootstrap 流程：

```bash
# 在 controller pod 内（kubectl exec）执行
kubectl -n dlw-prod exec -it deploy/dlw-controller -- \
  dlw admin bootstrap --oidc-subject "<your-oidc-sub>" --email "alice@example.com"
```

输出：

```
✓ Created system_admin user
  user_id:        1
  oidc_subject:   keycloak|auth0|abc-123
  email:          alice@example.com
  role:           system_admin
  created_at:     2026-05-07T...

You can now log in at https://dlw.example.com using OIDC.
```

> ⚠️ **bootstrap 仅可执行 1 次**：之后再调会报 "system_admin already exists, use UI to add more"。
> 如丢失 system_admin → 必须从 PG 直接 INSERT 紧急恢复（详见 §6 灾难恢复）。

---

## 4. dlw_admin_subject 怎么找

OIDC subject 是 IdP 内的稳定 user 标识符。各 IdP 不同：

- **Keycloak**：登录后 `https://keycloak.example.com/admin/master/console/#/dlw/users` → 用户详情页 → ID 字段
- **Auth0**：Dashboard → User Management → Users → 点击用户 → user_id 字段（如 `auth0|abc123`）
- **Okta**：profile → User → ID 字段
- **Google**：使用 email (sub claim 实际是数字串，但用 email 也可)

或最简单：先用普通 OIDC 流程（不带 bootstrap）登录一次，controller 会拒绝（"无 user 记录"），但日志里会打印你的 sub：

```bash
kubectl logs -l app.kubernetes.io/component=controller --tail=50 \
  | grep "oidc subject"
# OIDC subject 'keycloak|abc-123' attempted login but no user record exists.
# Use `dlw admin bootstrap` to create the first system_admin.
```

---

## 5. 后续 admin / 用户管理

第一个 system_admin 进来后，所有后续操作走 UI 或 CLI：

```bash
# 创建租户
dlw admin tenant create --slug team-a --display-name "AI Lab Team A"

# 创建 tenant_admin（已经在 IdP 注册过的用户）
dlw admin user create --tenant team-a \
  --oidc-subject "keycloak|bob-456" \
  --email "bob@example.com" \
  --role tenant_admin
```

---

## 6. 灾难恢复：丢失所有 system_admin

```bash
# 1. SSH 到 controller 主机或 PG 直连
kubectl -n dlw-prod port-forward svc/dlw-postgresql 5432:5432

# 2. 查找需要重建的 OIDC subject
psql -h localhost -U postgres dlw -c \
  "SELECT oidc_subject, email FROM users WHERE role='system_admin'"

# 3. 如果完全没 row：紧急 INSERT
psql -h localhost -U postgres dlw -c \
  "INSERT INTO users (tenant_id, oidc_subject, email, role, is_active)
   VALUES (1, '<your-oidc-sub>', 'admin@example.com', 'system_admin', true)
   RETURNING id"

# 4. 写 audit_log 标记紧急恢复
psql -h localhost -U postgres dlw -c \
  "INSERT INTO audit_log (tenant_id, action, resource_type, outcome, payload, prev_hash, self_hash)
   SELECT 1, 'admin.emergency_restore', 'users', 'success',
          '{\"recovered_oidc_subject\": \"<your-sub>\"}'::jsonb,
          (SELECT self_hash FROM audit_log ORDER BY id DESC LIMIT 1),
          encode(sha256('emergency_restore'::bytea), 'hex')"
```

---

## 7. 与其他文档的链接

- 多租户 / RBAC 设计：[`04-security-and-tenancy.md`](../v2.0/04-security-and-tenancy.md) §1
- OIDC + JWT 协议：[`02-protocol.md`](../v2.0/02-protocol.md) §1
- CLI auth：[`11-cli-and-sdk-spec.md`](../v2.0/11-cli-and-sdk-spec.md) §2.2
- 数据模型：[`01-architecture.md`](../v2.0/01-architecture.md) §4.1

---

## 附：local auth 备选（不依赖 OIDC）

如果你没有可用 OIDC IdP 或只是 POC 验证，可用本地用户名/密码认证。功能限制：
没有 SSO、没有 IdP 单点登出、不支持基于 IdP claims 的自动 tenant routing — 但
其他能力（多租户隔离、casbin RBAC、审计、device flow CLI）都正常。

**首次启动 bootstrap admin**：

```bash
# DLW_ADMIN_INITIAL_PASSWORD 仅在首次启动有效；admin 已存在时静默跳过
DLW_AUTH_DEV_MODE=true \
DLW_ADMIN_USERNAME=admin \
DLW_ADMIN_INITIAL_PASSWORD=ChangeMe-Strong-Pass-32+chars \
DLW_SYSTEM_JWT_SECRET=$(openssl rand -hex 32) \
uv run uvicorn dlw.main:app --port 8001
```

启动日志会出现 `bootstrapped local admin user 'admin'`。

**配置 alembic head**：用户表迁移 `a1b2c3d4e5f6_local_credentials.py` 必须先
跑：`uv run alembic upgrade head`。

**接口** (`/api/v1/auth/local/*`):

| Endpoint | 角色 | 用途 |
|----------|------|------|
| `POST /login` | 公开 | username/password → access_token (JWT, 1h TTL) + must_change_password 标志 |
| `POST /password` | 任何已登录用户 | 改自己的密码（需要 old + new） |
| `POST /users` | system_admin | 新建用户（指定 tenant_id + role + 初始密码） |
| `GET /users` | system_admin | 列出所有本地用户 |
| `POST /users/{id}/reset` | system_admin | 重置某用户的密码（用户下次登录需改密） |

**前端**：登录页（`/login`）已替换为 username/password 表单，OIDC 按钮保留；
Settings → "修改密码" 卡片所有用户可用，"用户管理" 卡片 system_admin 可见。

**AI 助手集成**：local auth 也是 AI 助手 `dlw_create_local_user` /
`dlw_reset_local_password` 写工具的底层 — 详见 [`MANUAL.md` § AI 助手](../../MANUAL.md)。

**Startup guard**：在非 dev 模式下，必须满足以下任一条件，否则 controller 会
拒绝启动：
- 配置了 OIDC（`DLW_OIDC_ISSUER` 非空），或
- 用了 local auth 且 `DLW_SYSTEM_JWT_SECRET` ≠ 默认占位值

**何时回到 OIDC**：上线企业部署、需要 SSO / SCIM / IdP-driven onboarding 时，
配置 OIDC 后两套机制可同时启用（local auth 作为 break-glass admin 通道）。
