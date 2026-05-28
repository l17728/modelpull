# modelpull QA 测试清单（测试人员手册）

> 用途：让测试人员**按图索骥**完整验证 modelpull v2.1 全部特性（含 v2.0 基座
> + AI Copilot + v2.1 五大特性）。每个测试包含：前置条件、操作步骤、期望结果、失败时怎么报。
> 适用版本：v2.1（Phase 1/2/3/4 + v2.1 全部 Sprint S1–S15）+ AI Copilot SP4f。
> 估时：完整跑一遍 ~ 1 工作日。

---

## 0. 前置：环境准备清单

测试前先把环境跑起来——二选一：**A) 本地 dev**（改代码 / 跑单测用）或 **B) 单机 docker 部署到服务器**（贴近生产的端到端测试用）。

### 0.A 本地 dev 环境

测试前确认以下都满足：

| 检查项 | 验证命令 | 期望 |
|--------|---------|------|
| PostgreSQL 18 在 :5433 可连 | `psql -h localhost -p 5433 -U postgres -c "SELECT 1"` | 返回 `1` |
| Python 3.12.x + uv | `uv --version && python --version` | uv ≥ 0.5、Python ≥ 3.12 |
| Node 20 + pnpm | `node -v && pnpm -v` | Node ≥ 20、pnpm ≥ 9 |
| Git 状态干净 | `git -C D:/download_weights status -s` | 空输出 |
| 最新 main | `git -C D:/download_weights log -1 --oneline` | 与远端 commit 一致 |
| 数据库迁移到 head | `uv run alembic upgrade head` | 无 error |
| Frontend 装好依赖 | `cd frontend && pnpm install` | 无 error |

**启动两个 server**（一个终端一个）：

```bash
# 终端 1: controller
cd D:/download_weights
DLW_AUTH_DEV_MODE=true \
DLW_SYSTEM_JWT_SECRET=qa-test-secret-32-bytes-long-pad! \
DLW_ADMIN_USERNAME=admin \
DLW_ADMIN_INITIAL_PASSWORD=admin1234 \
DLW_BEARER_TOKEN=qa-bearer-token \
DLW_DB_HOST=localhost DLW_DB_PORT=5433 \
DLW_DB_USER=postgres DLW_DB_NAME=dlw \
uv run uvicorn dlw.main:app --port 8001 --host 127.0.0.1

# 终端 2: frontend
cd D:/download_weights/frontend && pnpm dev
```

打开 <http://localhost:5173/>，用 `admin` / `admin1234` 登录。

### 0.B 单机 docker 部署（部署到服务器后再测）

要在一台 VM 上跑贴近生产的端到端测试时，用仓库自带的单机 bundle，**别手工拼各组件**。完整步骤见 [`deploy/single-host/README.md`](../../deploy/single-host/README.md)，最小路径：

```bash
# 本机打包 → scp 到 VM → 解压到 /opt/modelpull → 一键起
cd /opt/modelpull/deploy/single-host && sudo bash deploy.sh
# deploy.sh 会：装 docker(如缺) → bootstrap.sh 生成 .env + 打印一次性 admin 密码
#            → docker compose up -d --build → 等 controller healthy
```

起来后验证（controller 只绑 `127.0.0.1:8001`，对外走反代或 :5173）：

```bash
curl -s http://127.0.0.1:8001/health/ready    # 期望 {"status":"ready","db":"ok"}
curl -s http://127.0.0.1:8001/health/live     # 期望 {"status":"healthy"}
```

浏览器开 `http://<VM-IP>:5173/`（或你的反代域名），用 `admin` + bootstrap 打印的密码登录。

**国内 VM 部署必知**（不懂这些 build/pull 会卡到超时，是真实踩过的坑）：

| 坑 | 现象 | 应对 |
|----|------|------|
| Docker Hub 拉不动 | `docker compose pull` 卡 minio/postgres | `deploy.sh` 自动写 DaoCloud 镜像（`docker.m.daocloud.io`，实测唯一稳的；aliyun 镜像探测 200 但大文件限速 ~2KB/s） |
| **`uv sync` 巨慢**（构建镜像时） | 卡在 `Downloading botocore` 等 wheel 半天不动 | **换镜像源没用**——`uv.lock` 钉死 `files.pythonhosted.org` 的 wheel URL，`uv sync` 无视 `PIP_INDEX_URL`/`UV_INDEX_URL`，直连 PyPI 被限速。首次 build 在网络好的窗口做、耐心等；只改了少量代码时**别全量 rebuild**，用增量方式（见 README「Day-2 / Upgrades」） |
| 前端 dist 构建 OOM | 2GB VM 上容器内 `pnpm build` 被 Killed | **本机 `pnpm build` 后 scp dist 上去**（README「Frontend dist」段有命令），容器检测到 `dist/index.html` 就跳过构建 |
| pip/apt/npm 慢 | — | Dockerfile 已钉死：pip=清华、apt=阿里、npm=npmmirror，自动生效 |

**测试中出问题看日志**（路径都在 `deploy/single-host/logs/`）：

```bash
cd /opt/modelpull/deploy/single-host
bash logs.sh tail                 # 实时 tail controller.log
bash logs.sh tail executor-1      # 看某个 executor
bash logs.sh errors               # 最近 1 小时所有 WARNING/ERROR/Traceback
bash logs.sh snapshot             # 打 tarball，报 bug 时附上
```

> opencode（AI 助手后端）所需的 `opencode` 命令软链已 baked 进 controller 镜像，测试人员无需手工建。AI 助手的 LLM 连通性排查见 [AI 助手故障排查](./runbook-ai-assistant.md)。

---

## 1. 冒烟测试（先跑这 5 个，全过才往下走）

| # | 用例 | 步骤 | 期望 |
|---|------|------|------|
| S1 | 健康检查 | `curl http://127.0.0.1:8001/health/live` | `{"status":"healthy"}` |
| S2 | 数据库连接 | `curl http://127.0.0.1:8001/health/ready` | `{"status":"ready","db":"ok"}` |
| S3 | 登录 | 浏览器 → admin/admin1234 → 看到 Dashboard | 顶部出现 "租户 1 · system_admin" |
| S4 | 任务列表 | Dashboard 左侧 "任务" → 看到任务表（即使空） | 不报错，表头正确 |
| S5 | AI 助手抽屉 | 顶部 "🤖 AI 助手" → 抽屉展开 | 看到「🛠 可用工具」面板默认展开 + 21 个工具 |

任意失败 → 停止，按 [`runbook-ai-assistant.md`](./runbook-ai-assistant.md) §1 排查层级。

---

## 2. 认证 / 多租户（local auth + RBAC）

### 2.1 Local auth 基本流程

| # | 用例 | 步骤 | 期望 |
|---|------|------|------|
| A1 | 错误密码登录 | 输入 admin / wrongpass | 报 "用户名或密码错误"，不跳转 |
| A2 | 正确登录 | admin / admin1234 | 跳到 Dashboard，token 在 localStorage |
| A3 | 改密码 | Settings → 修改密码 → old=admin1234, new=NewStrongPass1! | 提示成功；登出后用旧密码失败，新密码成功 |
| A4 | 改回原密 | 再次走 A3，恢复 admin1234（方便后续用例） | 同上 |
| A5 | 新建用户 | Settings → 用户管理 → 新建 alice / Init1234, tenant=1, role=tenant_operator | 表格出现 alice，must_change_password=✓ |
| A6 | 新用户首次登录 | 登出 → alice / Init1234 | 跳 Dashboard 且抖出"首次登录请改密码"提示 |
| A7 | 重置他人密码 | admin 登录 → 用户管理 → alice 行点「重置密码」→ NewAlice1! | 成功；登出后 alice 用新密码可登录 |
| A8 | 非 admin 不能开用户管理 | tenant_operator 登录 → Settings → 看不到"用户管理"卡片 | 卡片整块隐藏 |

### 2.2 跨租户隔离

| # | 用例 | 步骤 | 期望 |
|---|------|------|------|
| A9 | 创建第二租户 | admin 用 SQL 加 `INSERT INTO tenants(id,slug,display_name) VALUES (2,'t2','T2')` | 成功 |
| A10 | 跨租户用户看不见对方 task | 在租户 1 创任务 X → 在租户 2 用 bob 登录 → /tasks 列表 | 看不到 X |
| A11 | 直接 GET /tasks/{X.id} 也 404 | 用 bob 的 token | 404 not found（不是 403） |

### 2.3 Startup guard

| # | 用例 | 步骤 | 期望 |
|---|------|------|------|
| A12 | 不设 jwt_secret 启动 | 去掉 `DLW_SYSTEM_JWT_SECRET`、`DLW_AUTH_DEV_MODE=false` 启动 | controller 拒绝启动，报 "insecure" |
| A13 | dev mode 允许默认值 | `DLW_AUTH_DEV_MODE=true` + 默认 jwt_secret | 启动成功，有 warning |

---

## 3. 任务下载主链路

### 3.1 基本创建 / 取消 / 删除

| # | 用例 | 步骤 | 期望 |
|---|------|------|------|
| T1 | 新建任务 | 任务页 → 新建 → `sentence-transformers/all-MiniLM-L6-v2` / main / storage_id=1 | 任务进列表，状态 `pending → scheduling → downloading` |
| T2 | 实时刷新 | 不刷新页面，等 30s 看状态 | 自动变化（SSE 实时推送） |
| T3 | 任务详情 | 点任务 → 4 个 tab：Files/Sources/Executors/Events | 每个 tab 都有数据，无 console error |
| T4 | 取消运行中任务 | T1 任务行 → 取消 | 状态变 `cancelling` 然后 `cancelled` |
| T5 | 删除终态任务 | T4 任务行 → 删除 | 行消失；列表更新 |
| T6 | 删除运行中报 409 | 创新任务 → 状态 running → 点删除 | 弹窗报错 `task_not_terminal` |
| T7 | 重试（新 task） | T5 删除前先选「重试」 | 创出新 task，repo_id 一样 |

### 3.2 多源加速（需要 ≥ 2 个 enabled source）

前置：`sources.yaml` 至少配 `huggingface` + `modelscope`（或镜像）。

| # | 用例 | 步骤 | 期望 |
|---|------|------|------|
| T8 | auto_balance 真用了多源 | 创任务后看 Sources tab | 看到 ≥ 2 个源在并行下载（不是 0 / 不是只 1 个） |
| T9 | 切 fastest_only | 改 source_strategy（API patch） | 下次 plan 后只用最快的源 |
| T10 | 拉黑某源 | patch source_blacklist=["modelscope"] | 不再分 chunk 到该源 |
| T11 | pin_huggingface | patch source_strategy=pin_huggingface | 只用 HF；HF 挂了任务变 `paused_external` |

### 3.3 增量下载（diff_and_dedup）

| # | 用例 | 步骤 | 期望 |
|---|------|------|------|
| T12 | 同 repo 同 revision 再下 | 完成 T1 后再创一模一样的 task | 新 task 所有子分片状态 `inherit`，几秒完成（不重下） |
| T13 | 升级到新 revision | upgrade_task 到不同 revision | 变化的文件下载，未变的文件 inherit |
| T14 | 不同 storage 下同 repo | 新 storage_id=2 上下载同 repo | 重新下载（不跨 storage 复用） |

### 3.4 配额

| # | 用例 | 步骤 | 期望 |
|---|------|------|------|
| T15 | 并发任务超配额 | 设 quota_concurrent=2，建 3 个 task | 第三个 task 报 `quota_exceeded`，不创建 |
| T16 | 月流量超配额 | 设 quota_bytes_month=1024，下个 1MB 模型 | 同上 |
| T17 | 配额查询 | 访问 /api/v1/quota/current | 返回 bytes_used / quota / storage 三组数 |

---

## 4. AI 助手（这次的重点新增）

### 4.1 工具帮助面板 + 决策链 UI

| # | 用例 | 步骤 | 期望 |
|---|------|------|------|
| AI1 | 工具列表完整 | 打开 AI 抽屉 → 「🛠 可用工具」面板默认展开 | 看到 21 个工具（11 read + 10 write），每个有图标 + 类别 tag + 描述 + 示例 |
| AI2 | 工具优先级提示 | 看面板顶部 intro | 包含"先用领域专用工具…再 web_search…最后回退模型记忆" |
| AI3 | footnote 警告 | 看面板底部 | 提到 web_search 需要 API key、否则可能产生幻觉 |

### 4.2 决策链（用 stub backend，可控可重现）

前置：`DLW_AI_BACKEND=stub`（默认）。

| # | 用例 | 步骤 | 期望 |
|---|------|------|------|
| AI4 | 任务查询触发工具 | 抽屉里发 "列出我的任务" | 决策链卡片出现 `📋 dlw_list_tasks(limit=20) [ok]`，回复带 "🤗 via internal data" 之类徽章 |
| AI5 | 模型记忆徽章 | 发 "讲个笑话"（不命中关键词） | 回复出现 `💭 来自模型训练记忆（可能过时）` 徽章 |
| AI6 | 工具失败徽章 | （故意 query 不存在的 task_id：show abcd-1234） | 决策链 `[failed]` + `⚠ 某次工具调用失败` 红徽章 |
| AI7 | 工具卡片展开 | 点决策链上的工具卡片 | 展开看到完整 input/output JSON |
| AI8 | 写工具确认卡 | 发 "下载 deepseek-ai/DeepSeek-R1" | 出现确认卡，listing repo_id/revision/storage_id |
| AI9 | 确认前后状态 | 确认卡片点「确认」 | 任务实际被创建（任务列表出现新行） |
| AI10 | 修改后确认 | 确认前点「修改」，改 revision=v1.0 | 任务用 v1.0 创建 |
| AI11 | 拒绝执行 | 点「拒绝」 | 没任务创建；helper 回复 "已取消" |

### 4.3 opencode backend（如果 opencode 装了）

前置：`DLW_AI_BACKEND=opencode`，opencode CLI 在 PATH，opencode 配了某个 LLM。

| # | 用例 | 步骤 | 期望 |
|---|------|------|------|
| AI12 | opencode 真调工具 | 发 "Hugging Face 上最新的 deepseek 模型有哪些" | 决策链出现 `🤗 search_huggingface_models(...) [ok]`；回复带 "🤗 via Hugging Face" 徽章 |
| AI13 | marker 解析正确 | 看 controller 日志的 stdout 行 | 看到 `[[dlw_tool name=... input=...]]` 标记被吃掉；前端看不到 marker 行 |
| AI14 | LLM 不打 marker 的退化 | 临时改 manifest 删 marker 指令 | UI 决策链空白；plain text 出现工具命令；说明 LLM 没遵守协议 → 加强提示 |

**如果 opencode 没装**：跳过 4.3 全部，记 N/A，不算 fail。

### 4.4 帮助 / 文档菜单（本次新增）

| # | 用例 | 步骤 | 期望 |
|---|------|------|------|
| AI15 | 文档抽屉打开 | 侧栏底部 "📚 文档" → 抽屉打开 | 左边列表显示 7 个文档（AI 排查、本地认证、SLA、可观测性、QA 清单、v2.1 部署清单、事故复盘模板），右边 Markdown 渲染 |
| AI16 | 切换文档 | 点列表别的项 | 右边内容切换 |
| AI17 | 帮助抽屉 | 侧栏底部 "📖 帮助" → 抽屉打开 | 显示 MANUAL.md 内容（不是缓存的老版本） |

---

## 5. 系统稳定性 / Recovery

### 5.1 Leader Election (active/standby)

需要起两个 controller 实例（不同端口，同 DB）。

| # | 用例 | 步骤 | 期望 |
|---|------|------|------|
| R1 | 双实例只一个 active | 起 :8001 和 :8002 | `/health/active` 一个 200 一个 503 |
| R2 | Kill active 后 standby promote | kill :8001 | 5s 内 :8002 的 `/health/active` 变 200 |
| R3 | Standby 进 recovery routine | 看 :8002 日志 | 出现 "running_recovery_routine" + 调和子分片状态 |

### 5.2 GC + 存储回收

| # | 用例 | 步骤 | 期望 |
|---|------|------|------|
| R4 | 删 task 后 storage_object 引用减 | T5 删除前后查 `subtask_object_refs` 行数 | 引用数 -N |
| R5 | 引用为 0 的 object 进 GC 候选 | 等下次 GC 跑 | object 被打 `tombstone` 或物理清掉（v2.1 物理 GC 后） |

### 5.3 Executor 上下线

| # | 用例 | 步骤 | 期望 |
|---|------|------|------|
| R6 | Executor 注册 | 起 `dlw-executor` 进程 | controller 日志看到注册，UI 执行器页出现 |
| R7 | Executor 失联 | kill executor | 任务的 chunk 在 grace 后重新分配 |
| R8 | 重启 executor 接回原任务 | 重起 executor | 不会重头下载已完成的 chunk |

---

## 6. 安全 / 审计

| # | 用例 | 步骤 | 期望 |
|---|------|------|------|
| SEC1 | mTLS 检查 | 不带证书直连 executor 接口 | 拒绝 |
| SEC2 | JWT 过期 | 用过期 token | 401 |
| SEC3 | 审计写入 | 创任务 → 查 `audit_log` | 看到 `task.create` 行 |
| SEC4 | 改 quota 进审计 | system_admin PUT /tenants/.../quota | `audit_log` 行 action=`tenant.quota.update`，payload 含 before/after |
| SEC5 | AI 工具调用进审计 | 通过 AI 取消任务 | action=`ai.tool.dlw_cancel_task`，payload.actor_kind=`ai_copilot` |
| SEC6 | T2 净化 bidi 字符 | 让 AI fetch 含 `‮` 字符的内容 | 内容 refused，不出现在 UI |
| SEC7 | 提示注入防护 | 让 AI fetch 含 "ignore previous instructions" 的页面 | 被 sanitize 标记，UI 显示 warning |

---

## 7. 性能基线（手动，不一定每次都跑）

| # | 用例 | 工具 | 期望 |
|---|------|------|------|
| P1 | API p95 < 300ms | `wrk -t4 -c20 -d30s --latency http://127.0.0.1:8001/api/v1/tasks` 带 token | p95 < 300ms |
| P2 | 单 executor 单源吞吐 ≥ 100MB/s | 下大模型，看 Sources tab 速率 | 视网络而定，至少不卡在 < 10 MB/s |
| P3 | 多源加速比 ≥ 1.5x | 同模型先 pin_huggingface 再 auto_balance | makespan 比例 |
| P4 | Failover RTO < 30s | 上面 R2 计时 | 真用秒表测一次 |

详细基线见 [`sla-slo.md`](./sla-slo.md) § 3。

---

## 8. 回归（每次 release 前必跑）

| # | 用例 | 命令 | 期望 |
|---|------|------|------|
| REG1 | 全 Python 单测 | `uv run pytest -q` | 867+ passed, 0 failed |
| REG2 | 全 Frontend 单测 | `cd frontend && pnpm test:unit -- --run` | 214+ passed |
| REG3 | 全 Frontend lint | `cd frontend && pnpm lint && pnpm typecheck` | 都过 |
| REG4 | Frontend build | `cd frontend && pnpm build` | 成功 |
| REG5 | 全 CI 在 GitHub | push → GitHub Actions | 12/12 jobs 全绿 |
| REG6 | Alembic round-trip | `uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head` | 无 error |

---

## 9. 失败报告模板

发现任一用例失败，按下面格式报：

```
【用例编号】 AI6
【环境】 git commit: <sha>; OS: Win11; PG 18 local
【步骤】 在 AI 抽屉发 "show abcd-1234"
【期望】 决策链显示 failed 徽章 + 红色 ⚠
【实际】 决策链没出现，回复直接是 "task not found"
【日志】 paste controller stderr 最近 50 行
【截图】 attach if UI 问题
```

---

## 10. v2.1 特性测试（已 ship — 跑在功能验证阶段）

v2.1.0-rc.1 已 cut（commit `acd553e`）。下列特性都已实现 + 全套 CI 通过；
测试人员在功能验证阶段把它们补到 §1-§9 的对应小节后跑：

| 特性 | Sprint | 验证位置 | 启用方式 |
|------|--------|---------|---------|
| SLA 分级 | S1 | §4 配额：先 `PUT /tenants/{id}/sla` 改为 `bulk`，制造系统忙到 91% 验证新任务 429 | 默认 ON，tenants 默认 `standard` |
| Physical GC | S3 | §6 增量下载后：删任务 → `POST /admin/gc/run` → 验证 storage 字节真减少 | `DLW_PHYSICAL_GC_ENABLED=true` |
| 跨地域复制 | S4-S6 | 新增 §10.1（见下） | `DLW_REPLICATION_WORKER_ENABLED=true` |
| 自适应优化 | S7-S9 | 新增 §10.2 | `DLW_ADAPTIVE_OPTIMIZER_ENABLED=true`（先 shadow）|
| 企业内网部署 | S10-S13 | 新增 §10.3 | 仅在内网 deploy 时启用 |

### 10.1 跨地域复制端到端

**前置**：至少 2 个 storage_backend、1 个已下载完成的 object（有 storage_object 行）。

**步骤**：
1. UI 左侧（admin 角色）→「跨地域复制」→ 看到空表 → 点「新建任务」
2. 填 `source_object_id` + `target_storage_id` → 确认 → 期望表中出现 pending 行
3. 等几秒（轮询间隔 5s）→ 状态变 `running` → 几秒后变 `succeeded`，`bytes_transferred` 显示完整 size
4. AI 助手提问"把 object {id} 复制到 storage {target_id}"→ 弹确认卡片 → 确认 → 创建第二个任务
5. 取消正跑的任务：点「取消」按钮 → 确认 → 期望状态变 `cancelled`，target 没有出现新对象

**期望**：`/metrics` 中 `dlw_replication_jobs_total{status="succeeded"}` 计数 +1，`dlw_replication_bytes_total` 增加；Grafana `dlw-replication` dashboard 有数据点。

### 10.2 自适应优化 shadow 验证

**前置**：至少 2 个 source（HF + mirror）+ ≥ 100 个已完成的 chunk（让 sampler 有数据）。

**步骤**：
1. 启动 controller 时设 `DLW_ADAPTIVE_OPTIMIZER_ENABLED=true`（apply 仍关）
2. 等 30s（replan_loop 一个 tick）→ controller 日志应有 `replan shadow: would move X/Y pending chunks (solve=N.NNNs, K capacity rows)`
3. `curl http://127.0.0.1:8001/metrics | grep dlw_replan_chunk_moves_total` 期望看到 `{mode="shadow"}` 计数 > 0、`{mode="apply"} = 0`
4. **不要直接启 apply**。Shadow 至少跑 24h 后人工对照 log 看 move 是否合理，再 set `DLW_ADAPTIVE_OPTIMIZER_APPLY=true`

**期望**：subtask_chunks.source_id 在 shadow 期间**不变**（直接查 DB 对照）；apply 启用后才看到 chunk 被迁移到 mirror。

### 10.3 反向 WSS + Live Console（仅企业内网 deploy）

**前置**：executor 启用 reverse WSS 客户端模式（v2.1 client 默认配置；详见 docs/operator/executor-runbook.md）。

**步骤**：
1. Executor 启动 → controller 日志应有 `reverse_ws: registered session ... for executor ex-N`
2. `curl -H "Authorization: Bearer $ADMIN_TOKEN" http://127.0.0.1:8001/api/v1/admin/reverse-ws/sessions` → 期望 items 含该 executor
3. 发 status 命令：`curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" -d '{"command":"status"}' http://127.0.0.1:8001/api/v1/admin/executors/ex-N/command` → 期望 200 + 返回 `command_id`
4. 试非白名单：`-d '{"command":"rm-rf"}'` → 期望 **422 NOT_WHITELISTED**
5. tenant_admin 角色（非 system_admin）试同样的请求 → 期望 **403**
6. Executor 进程 kill 重启 → 30s 内 sessions list 重新包含它（reconnect-wins）

**期望**：每条 command 都进审计日志；白名单严格只允许 `status` / `drain` / `restart`。

---

## 11. GA 验收（v2.1.0-rc.1 → v2.1.0 GA 切换流程）

> **本节是 ops 任务**，由运维 / SRE 在 staging 集群执行。完成 4 个子任务
> 后 release manager 才能 cut `v2.1.0` GA tag。
>
> 全程预计 7-10 个工作日（包含 7-day 压测 wall-time）。

### 11.1 任务 1 — 7-day Locust 压测

**前置（D-1，开跑前一天）**：

1. staging 集群验证：
   ```bash
   kubectl -n dlw-staging get pods -l app=dlw-controller
   # 期望 2 个 pod ready (active + standby)
   kubectl -n dlw-staging get statefulset dlw-executor
   # 期望 ≥ 10 replicas
   ```

2. 拿两个 JWT（tenant + admin）写入环境变量：
   ```bash
   # 在 controller pod 内执行
   kubectl -n dlw-staging exec deploy/dlw-controller -- \
     ./deploy/runbooks/scripts/maintenance.sh --issue-jwt tenant_admin > /tmp/tenant.jwt
   kubectl -n dlw-staging exec deploy/dlw-controller -- \
     ./deploy/runbooks/scripts/maintenance.sh --issue-jwt system_admin > /tmp/admin.jwt
   ```

3. 安装 Locust + 准备机器（建议跟 staging 同 region 的小 EC2 / VM，不要本机）：
   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install 'locust>=2.30'
   ```

4. Grafana：打开 `dlw-overview` + `dlw-replication` dashboard，时间窗调到 `last 7 days`，准备截图基线。

**执行（D0）**：

```bash
export DLW_BASE_URL=https://staging.modelpull.internal
export DLW_JWT=$(cat /tmp/tenant.jwt)
export DLW_ADMIN_JWT=$(cat /tmp/admin.jwt)

# 后台启动，stdout 重定向到日志
nohup locust -f deploy/loadtest/locustfile.py \
       --headless -u 100 -r 10 -t 7d \
       --csv staging-baseline \
       --html staging-baseline.html \
       > locust.log 2>&1 &
echo "pid=$!" > locust.pid
```

参数说明：
- `-u 100`：100 并发虚拟用户（4 user type 按 weight 分配：60+20+15+5）
- `-r 10`：每秒 spawn 10 个 user（10 秒到峰值，避免 thundering herd）
- `-t 7d`：跑满 7 天后自动停
- `--csv` + `--html`：自动产生 per-request + 总报告

**每日检查（D1 - D7）**：

```bash
# 看进度
tail -20 locust.log

# 看错误率（关键指标）
grep -c "POST /api/v1/tasks .* 5[0-9][0-9]" locust.log

# 看 controller 健康
curl -s $DLW_BASE_URL/health/ready | jq .
```

如出现以下情况**立即停止**（`kill $(cat locust.pid)`）+ 调查：
- controller `/health/ready` 持续 503 超过 3 分钟
- 任何 endpoint 的 5xx 率 > 1%
- p95 延迟突然飙到 > 1s 持续 > 10 分钟（非 chaos drill 期间）

**评分（D7 结束）**：

打开 `staging-baseline.html` 检查 4 个 acceptance gate：

| Gate | 测量 | 通过标准 | 失败处理 |
|------|------|---------|---------|
| Availability | controller `/health/ready` 7 天总宕机 | 0 段连续 > 3 min | 看 Grafana, 找根因 |
| Latency | p95 per-endpoint（按小时分桶） | < 300 ms（除 chaos drill 期间） | 慢查询调查 |
| Throughput | task-create QPS sustained | ≥ 5（不含 quota 4xx） | 调度器 profiling |
| Data integrity | 7 天后所有 task 是否都有终态 | 0 行 `running` > 2h | 单独跑 recovery |

4 个全过 → 标记任务 1 ✅。任意 fail → 写 post-mortem（`docs/operator/post-mortem-template.md`）+ 修 + 重跑。

### 11.2 任务 2 — 4 个 chaos drill

按 [`deploy/runbooks/chaos-drill.md`](../../deploy/runbooks/chaos-drill.md)
执行，**与 11.1 的 Locust 同时跑**（chaos 是在压测背景下注入的）。

**操作流程**：

1. **每个 drill 前**：通知 `#modelpull-staging` 频道，注明 drill ID + 预计开始时间 + 影响范围（"staging 集群可能 5 分钟不可用"）。
2. **执行 drill**：严格按 runbook 步骤；不要跳步、不要"试试看"。
3. **观察期 5-10 min**：watch Grafana + tail `locust.log`，记录每个 acceptance check 的实际值。
4. **每个 drill 后**：
   - 填 `docs/operator/post-mortem-template.md` —— 即使 drill 顺利通过也写一份"演练记录"，证明流程跑过
   - 如果 drill 触发了真问题，标 SEV，按事故流程走
5. **Drill 间隔**：每个 drill 之间留 30 min 让系统稳定，再做下一个。

**4 个 drill 的预期结果矩阵**：

| Drill | 关键指标 | 通过 | 失败响应 |
|-------|---------|------|---------|
| 1. PG 拔插 | `/health/ready` 5 分钟内恢复 200，无 task 卡 `running` | drill 1 ✅ | 检查 PG 重连配置 |
| 2. 拔 S3 region | 失败 replication job retry_count 触顶 = 3 不无限重试；恢复后手动建的新 job 成功 | drill 2 ✅ | 看 replication_worker 日志 |
| 3. Kill active controller | 30 秒内 standby promote；任务不报 failed | drill 3 ✅ | 看 leader_election 日志 |
| 4. Mass executor disconnect | 2 分钟内 sessions list 重新满 | drill 4 ✅ | 看 reverse_ws_registry 日志 |

4 个全过 → 标记任务 2 ✅。

### 11.3 任务 3 — 填回 sla-slo.md § 3 容量基线

打开 [`sla-slo.md`](./sla-slo.md) § 3，把 5 行 ❓ 替换为 11.1 + 11.2 的实测值：

```diff
- | 单 controller 并发 task | ≤ 500 | ❓ 待 Sprint 15 压测填回 |
+ | 单 controller 并发 task | ≤ 500 | ✅ <实测峰值> sustained (locust run YYYY-MM-DD) |

- | 单 controller 并发 executor | ≤ 100 | ❓ 待 Sprint 15 压测填回 |
+ | 单 controller 并发 executor | ≤ 100 | ✅ <实测数> (drill 4 验证) |

- | 租户数 | ≤ 1000 | ❓ casbin 在该规模延迟未实测 |
+ | 租户数 | ≤ 1000 | ✅ <数> 户 × casbin p95 <ms>ms |

- | **v2.1 新增 — 反向 WSS 并发** | ≤ 200 sessions / controller | ❓ ... |
+ | **v2.1 新增 — 反向 WSS 并发** | ≤ 200 sessions / controller | ✅ <实测峰值> |

- | **v2.1 新增 — chunk_throughput_sample 写入率** | ≤ 1000 rows/s sustained | ❓ ... |
+ | **v2.1 新增 — chunk_throughput_sample 写入率** | ≤ 1000 rows/s sustained | ✅ <实测平均> rows/s |
```

实测值从哪里来：
- Locust HTML：`Total Requests / Total Time` = 平均 QPS；`Median Response Time` / `95% Response Time` 是 p50/p95
- Grafana：`dlw-overview` dashboard 的 `dlw_tasks_active_count` 峰值 = 并发 task 峰值
- `kubectl -n dlw-staging exec deploy/dlw-controller -- curl -s localhost:8001/metrics | grep dlw_replication_bytes_total` 取 throughput 累计 ÷ 7 天 = 平均吞吐
- PG 写率：`kubectl -n dlw-staging exec sts/pg -- psql -c "SELECT now()-stats_reset, tup_inserted/extract(epoch from now()-stats_reset) FROM pg_stat_database WHERE datname='dlw'"`

**Commit + push**：
```bash
git checkout -b chore/v21-baseline-fillback
git add docs/operator/sla-slo.md
git commit -m "chore(v2.1): fill in sla-slo capacity baseline from staging run

Numbers measured 2026-MM-DD on staging集群 N-controller M-executor
configuration. Locust run ID: staging-baseline-YYYY-MM-DD."
gh pr create --title "chore(v2.1): sla-slo baseline from staging" --base main
```

PR merge 后 → 标记任务 3 ✅。

### 11.4 任务 4 — Cut v2.1.0 GA tag

**前置**：11.1-11.3 三个任务都 ✅；release manager 在 `#modelpull-announce` 已宣告 GA 窗口。

**步骤**：

1. **更新 CHANGELOG**：
   ```bash
   # 把 [v2.1.0-rc.1] 标题改为 [v2.1.0]，加 GA 行
   vim CHANGELOG.md
   ```
   ```diff
   - ## [v2.1.0-rc.1] — 2026-05-27
   + ## [v2.1.0] — 2026-MM-DD (GA)
   +
   + RC1 在 staging 7-day 压测 + 4 chaos drill 全过 (locust run ID:
   + staging-baseline-YYYY-MM-DD)。容量基线已填回 sla-slo.md § 3。
   ```

2. **更新 release notes**：
   ```bash
   cp docs/releases/v2.1.0-rc.1.md docs/releases/v2.1.0.md
   # 编辑顶部 GA gate 部分，从 "pending" 改为 "completed with measured numbers"
   vim docs/releases/v2.1.0.md
   ```

3. **Commit + push + tag**：
   ```bash
   git add CHANGELOG.md docs/releases/v2.1.0.md
   git commit -m "release: v2.1.0 GA"
   git push origin main

   # 等 CI 绿后
   git tag -a v2.1.0 -m "v2.1.0 GA — see docs/releases/v2.1.0.md"
   git push origin v2.1.0
   ```

4. **GitHub release**：
   ```bash
   gh release create v2.1.0 \
     --title "v2.1.0 — GA" \
     --notes-file docs/releases/v2.1.0.md
   ```

5. **Helm chart bump**：
   ```bash
   sed -i 's/^version:.*/version: 2.1.0/' deploy/helm/Chart.yaml
   sed -i 's/^appVersion:.*/appVersion: "2.1.0"/' deploy/helm/Chart.yaml
   git add deploy/helm/Chart.yaml
   git commit -m "release: bump helm chart to 2.1.0"
   git push origin main
   ```

6. **公告**：在 `#modelpull-announce` 发布：
   ```
   🎉 v2.1.0 GA shipped (commit <SHA>)
   • GitHub release: <URL>
   • 5 大特性 (SLA / GC / Replication / Optimizer / Enterprise net)
   • Staging baseline filled: <link to sla-slo.md PR>
   • 升级文档: docs/operator/v21-production-deployment.md
   On-call paged for 48h post-GA monitoring.
   ```

7. **48h on-call 监控**：cut 后两天严密看 Grafana。任何 SLO miss 立即 SEV2 起，post-mortem 走 §post-mortem-template 流程。

---

*最后更新：2026-05-27（v2.1.0-rc.1 cut；§10 重写为已 ship 特性、§11 新增 GA 验收 ops 流程）。维护：本文随核心特性 PR 同步更新；测试人员发现遗漏的场景请提 PR / issue 补到对应章节。*
