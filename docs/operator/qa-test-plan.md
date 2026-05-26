# modelpull QA 测试清单（测试人员手册）

> 用途：让测试人员**按图索骥**完整验证 modelpull v2.0 + 本次 AI Copilot
> 增量。每个测试包含：前置条件、操作步骤、期望结果、失败时怎么报。
> 适用版本：v2.0 (Phase 1/2/3/4) + AI Copilot SP4f。
> 估时：完整跑一遍 ~ 1 工作日。

---

## 0. 前置：环境准备清单

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

---

## 1. 冒烟测试（先跑这 5 个，全过才往下走）

| # | 用例 | 步骤 | 期望 |
|---|------|------|------|
| S1 | 健康检查 | `curl http://127.0.0.1:8001/health/live` | `{"status":"healthy"}` |
| S2 | 数据库连接 | `curl http://127.0.0.1:8001/health/ready` | `{"status":"ready","db":"ok"}` |
| S3 | 登录 | 浏览器 → admin/admin1234 → 看到 Dashboard | 顶部出现 "租户 1 · system_admin" |
| S4 | 任务列表 | Dashboard 左侧 "任务" → 看到任务表（即使空） | 不报错，表头正确 |
| S5 | AI 助手抽屉 | 顶部 "🤖 AI 助手" → 抽屉展开 | 看到「🛠 可用工具」面板默认展开 + 18 个工具 |

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
| AI1 | 工具列表完整 | 打开 AI 抽屉 → 「🛠 可用工具」面板默认展开 | 看到 18 个工具（11 read + 7 write），每个有图标 + 类别 tag + 描述 + 示例 |
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
| AI15 | 文档抽屉打开 | 侧栏底部 "📚 文档" → 抽屉打开 | 左边列表显示 4 个文档（AI 排查、本地认证、SLA、QA 清单），右边 Markdown 渲染 |
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

## 10. v2.1+ 待测特性（未来 release）

⚠️ **现在不要测**，等对应 feature 实现：

- 物理字节 GC + LRU 驱逐（已有 services/physical_gc.py 骨架，详见
  [`v2.1-roadmap.md`](../v2.1-roadmap.md)）
- 自适应运筹优化（v2.1）
- 企业内网部署（反向 WSS / 凭证池 / Live Console）
- 跨地域复制
- SLA 分级

---

*最后更新：2026-05-26。维护：本文随核心特性 PR 同步更新；测试人员发现遗漏的
场景请提 PR / issue 补到对应章节。*
