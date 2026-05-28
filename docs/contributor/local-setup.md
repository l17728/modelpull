# Local Development Setup

> 用途：让贡献者第一天就能 self-serve 跑通本地环境。
> 覆盖完整的 backend + frontend dev 栈（v2.0 + v2.1 已实现）以及 CI 工具链。

---

## 1. 仓库现有可执行物

| 区域 | 路径 | 依赖 |
|------|------|------|
| 后端 (FastAPI + SQLAlchemy + alembic) | `src/dlw/` | Python ≥ 3.12, uv, PostgreSQL 18 |
| 前端 (Vue3 SPA) | `frontend/` | Node ≥ 20, pnpm |
| 测试 (1053 后端 + 219 前端) | `tests/`, `frontend/src/**/*.spec.ts` | pytest, vitest |
| 不变量护栏 | `tools/lint_invariants.py` (+ unit tests) | Python |
| OpenAPI 3.1 spec | `api/openapi.yaml` | `npx @redocly/cli` 或 swagger-cli |
| Helm chart + 告警 + dashboard | `deploy/helm/`, `deploy/prometheus/`, `deploy/grafana/` | helm |
| 单机 docker compose 部署 | `deploy/single-host/` | docker compose |

---

## 2. 本地 dev 栈 setup

```bash
# 1. clone + Python 工具链
git clone https://github.com/l17728/modelpull && cd modelpull
python --version      # 3.12+
pip install uv

# 2. 后端依赖 + DB schema（PostgreSQL 18 跑在 :5433）
uv sync
DLW_DB_HOST=localhost DLW_DB_PORT=5433 DLW_DB_USER=postgres DLW_DB_NAME=dlw \
  uv run alembic upgrade head

# 3. 起 controller（dev 模式，最小环境变量）
DLW_AUTH_DEV_MODE=true \
DLW_SYSTEM_JWT_SECRET=dev-secret-32-bytes-long-padding! \
DLW_ADMIN_USERNAME=admin DLW_ADMIN_INITIAL_PASSWORD=admin1234 \
DLW_DB_HOST=localhost DLW_DB_PORT=5433 DLW_DB_USER=postgres DLW_DB_NAME=dlw \
  uv run uvicorn dlw.main:app --port 8001 --host 127.0.0.1

# 4. 起前端（另一个终端；vite proxy 默认指向 :8001）
cd frontend && pnpm install && pnpm dev   # http://localhost:5173

# 5. 跑测试
uv run pytest                              # 后端全套
cd frontend && pnpm test                   # 前端 vitest

# 6. 不变量 lint + 自身单测
python tools/lint_invariants.py            # OK: 46 invariants ... contiguous
uv run pytest tools/test_lint_invariants.py -v

# 7. (可选) Helm / OpenAPI lint
helm lint deploy/helm/
npx @redocly/cli preview-docs api/openapi.yaml
```

> 完整测试人员手册（含 v2.1 全部特性的逐项验证步骤）见 [`docs/operator/qa-test-plan.md`](../operator/qa-test-plan.md)，
> UI 内「📚 文档」抽屉也能直接打开。

---

## 3. 你的第一个 PR

最容易上手的 PR 类型：

### 3.1 修文档错别字 / 死链 / 编号

1. CI 跑 markdown lint + lychee link check + invariant lint
2. 改完本地跑：
   ```bash
   python tools/lint_invariants.py            # 不变量 / cross-ref
   # markdown lint（可选，需 markdownlint-cli2）
   npx markdownlint-cli2 docs/v2.0/*.md README.md
   ```
3. 提 PR；CI 必须全绿

### 3.2 在不变量索引中加 / 删一条

1. 在合适的章节用 `🔒 不变量 N: ...` 声明
2. 在 `docs/v2.0/01-architecture.md §7` 表加一行
3. CI invariant_lint 会校验编号唯一 + 索引完整 + 索引无 gap
4. 加测试用例 `tools/test_lint_invariants.py` 如有边界场景

### 3.3 提交 design review issue → PR fix

1. 看现有 INDEX 修改日志，找 reviewer 已发现但 still open 的 issue
2. 对照 fix 写 PR
3. PR 描述里引用对应 reviewer ID（如 `Fixes #42 (CODE-V21-09)`）

---

## 4. 单机一键栈（docker compose）

不想手工起各组件时，用单机 compose bundle 一次拉起 PG + MinIO + controller + 2 executor + 前端：

```bash
cd deploy/single-host
# 按 README 配好 .env（DB 密码 / JWT secret / 可选 AI key），然后：
docker compose up -d
```

详见 [`deploy/single-host/README.md`](../../deploy/single-host/README.md)（含国内 VM 镜像源 checklist + 低内存机器先本地 build 前端 dist 的指引）。

---

## 5. PR 流程

详见 [CONTRIBUTING.md](../../CONTRIBUTING.md)。简版：

1. Fork
2. 分支命名：`docs/<topic>` / `fix/<topic>` / `feat/<topic>` / `ci/<topic>`
3. PR 标题：`[area] short description`（例 `[docs] fix invariant 32 numbering`）
4. PR 描述：
   - `Fixes #N` 或 `Refs #N`
   - 影响范围 checkbox
   - 如改 DB schema：必须更新 `01 §4`
5. CI 全绿才会 review
6. Squash merge

---

## 6. troubleshooting

### invariant lint 失败

```
FAIL: 1 invariant lint failures
  ✗ Invariants declared in body but missing from §7 index table: [47]
```

→ 你在某文件加了 `🔒 不变量 47` 但 §7 表没有；补行到 `docs/v2.0/01-architecture.md §7` 表。

### lychee link check 失败

→ markdown link 失效；用相对路径 `./05-operations.md` 而非绝对 URL。锚点改用全小写（GitHub 自动化 anchor 规则）。

### Helm template 失败

```
Error: execution error at (dlw/templates/secretproviderclass.yaml:12:21):
  .Values.controller.secrets.vault.address required
```

→ 默认 values 用 `vault.enabled: false`；如果你启用了 Vault，必须 `--set controller.secrets.vault.address=...` 或在 values.production.yaml 配置。

### gitleaks 误报示例 token

→ 用明显占位符如 `<TOKEN_HERE>` 而非看似真实的 hex 串（如 `ent_a1b2c3...`）

---

## 7. 联系

- 设计讨论：[GitHub Discussions](https://github.com/l17728/modelpull/discussions)
- Bug：[Issue templates](https://github.com/l17728/modelpull/issues/new/choose)
- 紧急（如果你确实需要）：通过 issue 标 `priority: urgent`
