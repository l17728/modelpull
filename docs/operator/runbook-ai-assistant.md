# Runbook — AI Copilot 故障排查

> 适用：AI 助手抽屉打不开 / 永远返回错误 / 决策链 UI 空白 / 工具调用没生效。
> 估时：5-30 min（视故障层）。

---

## 1. 先判断故障层

AI 助手的请求链路：

```
浏览器
  ↓ POST /api/v1/ai/chat (SSE)
controller (FastAPI)
  ↓ runner = build_runner(settings)
  ↓ 根据 DLW_AI_BACKEND 选 stub | opencode
[stub 模式: 进程内执行]    [opencode 模式: subprocess]
  ↓ 工具调用                ↓ opencode CLI 子进程
in-process Tool.run()      opencode → 后端 LLM → stdout
```

故障**必落在以下 5 层之一**，按从外到内排查：

| 层 | 测试方法 | 典型症状 |
|----|---------|---------|
| 1. 浏览器 / WebSocket | DevTools 看 Network /ai/chat 状态码 | 助手抽屉转圈不停 |
| 2. controller HTTP | `curl http://127.0.0.1:8001/health/live` | 502 / 连接失败 |
| 3. AI router 本身 | `curl -X POST .../ai/chat -d '{"message":"hi"}'` | 500 / 配额拒绝 |
| 4. runner backend | 看日志 `dlw.ai.runner` 模块 | "AIBackendUnavailable: opencode not on PATH" |
| 5. 工具调用 | 看日志 `dlw.ai.tools` / `dlw.services.*` | 单个工具报 error |

---

## 2. 高频故障 + 对策

### 2.1 浏览器报 "网络错误，请检查连接"

**根因 95%**：vite proxy 转发到的端口上 controller 没起，或起的是**旧代码进程**没有当前路由。

**修**：
```powershell
# 看 8001 是谁
Get-NetTCPConnection -LocalPort 8001 -State Listen |
  ForEach-Object { Get-Process -Id $_.OwningProcess | Select StartTime,Id }

# 看 controller 路由有没有 /api/v1/ai/chat
curl -s http://127.0.0.1:8001/openapi.json | jq '.paths | keys[]' | grep ai
```

如果路由缺失 → 旧进程跑老代码。kill + 重启（见
[`local auth port-proxy memory`](../../CHANGELOG.md) 类似经验）：

```powershell
$pid = (Get-NetTCPConnection -LocalPort 8001).OwningProcess; Stop-Process -Id $pid -Force
cd D:\download_weights
DLW_AUTH_DEV_MODE=true DLW_SYSTEM_JWT_SECRET=... uv run uvicorn dlw.main:app --port 8001
```

### 2.2 助手回复永远是 "(stub) You said: ..." 或工具不调

**根因**：`DLW_AI_BACKEND=stub` 配置生效中（默认值）。stub backend 只做关键词路由，不接真 LLM。

**修**（前提：opencode CLI 已安装在 PATH）：

```bash
export DLW_AI_BACKEND=opencode
export DLW_AI_OPENCODE_BIN=opencode   # 或绝对路径
# opencode 自己的 LLM 配置（API key 等）通过 opencode 自己的 ~/.config/opencode 管理
# modelpull 不需要 ANTHROPIC_API_KEY / OPENAI_API_KEY
```

重启 controller。验证：

```bash
curl -X POST http://127.0.0.1:8001/api/v1/ai/chat \
  -H 'Authorization: Bearer <token>' -H 'Content-Type: application/json' \
  -d '{"message":"列出我的任务"}' -N
# 应该看到 tool_call 事件而不是 "(stub)..."
```

### 2.3 助手回复 "AIBackendUnavailable: opencode binary not found on PATH"

**修**：
```bash
which opencode || npm i -g opencode-ai   # 视实际安装方式而定
# 或在 .env 里指定绝对路径
DLW_AI_OPENCODE_BIN=/usr/local/bin/opencode
```

### 2.4 决策链 UI 一直空 / 不显示工具调用（opencode 后端）

**根因**：opencode 后端通过 `[[dlw_tool ...]]` 标记触发决策链事件。若 LLM **没按 manifest 要求打标记**，UI 就空。

**诊断**：
1. 看 controller stderr — 有没有 `assistant.message_delta` 事件流出而 `tool_call` 一直没有？
2. 看 opencode 原始 stdout（可临时改 `runner.py` 打印未解析的行）— 有没有 `[[dlw_tool` 这种行？
3. 如果完全没有 → LLM 忽略了 manifest 指令。可能原因：
   - LLM context window 不够（manifest ~2600 token）
   - LLM 模型太小，不遵循 system prompt
   - opencode CLI 自己也插入了 system prompt 覆盖

**对策**：
- 换更大的 LLM 模型
- 暂时禁用 skills 注入：`DLW_AI_OPENCODE_INJECT_SKILLS=false` —
  助手会回退到 plain Q&A（**没有工具调用**，仅当 LLM 训练数据用），但至少能聊
- 长期：考虑 [真正的 MCP server](https://github.com/modelcontextprotocol/spec) 替代 — 不过这是反向决策（之前已从路线图删除，详见 CHANGELOG）

### 2.5 单个工具报 error（其他工具正常）

例如 `web_search` 失败但 `dlw_list_tasks` 正常 → 看具体工具是否 operator-gated：

```bash
# web_search 需要 Brave API key
echo $DLW_AI_WEB_SEARCH_API_KEY
# fetch_user_content 需要 hostname allowlist
echo $DLW_AI_FETCH_USER_CONTENT_HOSTNAMES
```

工具描述会主动返回 `{"error": "disabled by operator (no API key)"}` 类错误 — 这是设计行为，**不是 bug**，按需配置即可。

### 2.6 AI Token 配额耗尽

```
quota_exceeded event in SSE stream
前端弹 ElAlert: "本月 AI Token 用量已超限"
```

**修**（system_admin）：

```bash
curl -X PUT http://127.0.0.1:8001/api/v1/tenants/<id>/quota \
  -H 'Authorization: Bearer <admin-token>' -H 'Content-Type: application/json' \
  -d '{"quota_ai_tokens_month": 5000000}'   # 调高上限
```

或让用户等下月配额重置（每月 1 号 UTC）。

---

## 3. 完全降级路径

如果以上都修不好，AI 助手不可用 **不影响下载主链路**：

- 用户仍可走前端任务列表、CLI、API 完成所有下载操作
- 把抽屉入口隐藏：在 nav registry 里临时移除 AI 入口
- 系统其他功能（任务调度、多源、配额、审计）完全独立于 AI 模块

把 controller 启动时的 AI 模块全关：

```bash
# 任何 ai_backend 值都至少能进 stub 模式；要"完全关掉"目前只能屏蔽前端入口。
# 后端 /api/v1/ai/* 路由始终注册，但 stub 模式不消耗外部资源。
```

---

## 4. 日志位置（按服务）

- `dlw.ai.service` — 对话持久化 / 配额检查
- `dlw.ai.runner` — backend 选择 / opencode subprocess
- `dlw.ai.tools` — 读工具执行
- `dlw.ai.write_tools` — 写工具 + 确认
- `dlw.services.local_auth` — 见 [runbook-local-auth.md](./runbook-local-auth.md)

启用 DEBUG 级别：

```bash
PYTHONLOGLEVEL=DEBUG uv run uvicorn dlw.main:app --port 8001
```

---

## 5. 联系上游

- modelpull 仓库：<https://github.com/l17728/modelpull/issues>
- opencode 自己的问题：<https://github.com/sst/opencode/issues>
