# Local Development Setup

> 用途：让贡献者第一天就能 self-serve 跑通本地环境。
> Phase 1 启动后会扩展为完整的 backend + frontend dev 环境；现在只覆盖**当前可跑的 CI 工具链**。

---

## 1. 当前能本地跑的（设计阶段）

仓库现有可执行物：

| 工具 | 用途 | 依赖 |
|------|------|------|
| `tools/lint_invariants.py` | 校验 46 不变量索引一致性 + 跨文档引用 | Python ≥ 3.9 |
| `tools/test_lint_invariants.py` | 9 个 pytest unit test for 上面的 lint | pytest |
| `deploy/runbooks/scripts/*.sh` | 7 个 ops runbook 脚本（无法本地运行真任务，只能 review 代码） | bash, optional psql / kubectl |
| `api/openapi.yaml` | OpenAPI 3.1 spec | `npx @redocly/cli` 或 swagger-cli |
| `deploy/helm/` | Helm chart | `helm` |
| `deploy/prometheus/`, `deploy/grafana/` | 告警规则 + dashboard JSON | (review only) |

后端代码（FastAPI / SQLAlchemy / Vue3）等 Phase 1 启动后再加。

---

## 2. 5 分钟本地 setup

```bash
# 1. clone
git clone https://github.com/l17728/modelpull
cd modelpull

# 2. Python 工具链（3.9+）
python3 --version
pip install pytest

# 3. Node（用于 Helm / OpenAPI lint，可选）
node --version    # 18+

# 4. 跑 invariant lint（最小验证仓库未坏）
python tools/lint_invariants.py
# 期望：OK: 46 invariants declared, 46 indexed in 01 §7, contiguous

# 5. 跑 lint 工具自身的单测
python -m pytest tools/test_lint_invariants.py -v
# 期望：9 passed

# 6. (可选) Helm chart lint
helm version    # 3.14+
helm dependency update deploy/helm/  # 拉 PG / Prometheus / Grafana 子 chart
helm lint deploy/helm/
helm template dlw deploy/helm/ > /tmp/rendered.yaml
# 检查 rendered.yaml 是否符合预期

# 7. (可选) OpenAPI 浏览
npx @redocly/cli preview-docs api/openapi.yaml
# 浏览器自动打开 http://localhost:8080
```

---

## 3. 你的第一个 PR（design 阶段）

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

## 4. dev 环境（Phase 1 启动后扩展）

预计 Phase 1 Week 1-2 提供以下：

```bash
# (Phase 1 后) 本地开发栈
docker-compose -f deploy/dev/docker-compose.yml up -d
# 起 PG + MinIO + wiremock(HF mock) + controller + 1 executor
```

```bash
# (Phase 1 后) 后端 dev
cd backend
uv sync                         # 装依赖
uv run alembic upgrade head     # DB schema
uv run uvicorn dlw.main:app --reload
```

```bash
# (Phase 1 后) 前端 dev
cd frontend
pnpm install
pnpm run dev                    # http://localhost:5173
```

详细要等 Phase 1 启动。届时本文会更新。

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
