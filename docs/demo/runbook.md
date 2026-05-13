# modelpull Phase 1 Alpha Demo Runbook

> 单租户 PoC：UI 创建任务 → controller 调度 → executor 拉 HF → 流式上传 S3 → sha256 校验 → 任务终态。
> 全栈 docker-compose 一键起；浏览器看任务实时进度；MinIO 看真实落地的对象。

---

## 0. 前置

| 依赖 | 版本 | 验证 |
|------|------|------|
| Docker + Docker Compose | 任意近版 | `docker compose version` |
| 端口 | 5433/8000/9000/9001/5173 free | `ss -ltn` |
| Node + pnpm（仅 UI 演示需要） | Node ≥ 20 / pnpm ≥ 9 | `node --version && pnpm --version` |

`DLW_BEARER_TOKEN` 默认 `dev-token-change-me`（与 `docker-compose.dev.yml` 一致）。生产换。

---

## 1. 一键脚本（推荐）

```bash
./scripts/demo-alpha.sh
```

脚本做 5 步：

1. `docker compose -f docker-compose.dev.yml up -d --build` 启 5 个服务
2. 轮询 `/health/ready` 直到 controller 就绪
3. POST `/api/v1/tasks` 创建任务（默认 `sentence-transformers/all-MiniLM-L6-v2` 公开模型 ~90MB / 多文件）
4. 每 2 秒查 status 直到 `succeeded` / `failed` / `cancelled`（默认超时 180s）
5. 打印总结：每个 subtask 的 status + s3_key

预期输出片段：

```
[1/5] Boot stack ...
[2/5] Wait for controller /health/ready ...
      controller ready
[3/5] POST /api/v1/tasks ...
      task_id = 27eb6723-9d24-41fc-9dd3-98bf9ba28999
[4/5] Poll until terminal state (timeout=180s)...
      [19:54:12] status: pending
      [19:54:14] status: scheduling
      [19:54:18] status: downloading
      [19:55:43] status: succeeded
[5/5] Summary:
      task          27eb6723…
      repo          sentence-transformers/all-MiniLM-L6-v2@main…
      status        succeeded
      subtasks      8
      ✓ succeeded   8/8
        succeeded  config.json                    → phase1/sentence-transformers/all-MiniLM-L6-v2/main/config.json
        succeeded  model.safetensors              → phase1/.../model.safetensors
        ...
```

---

## 2. UI 演示（可选 — 直观看进度）

新开一个终端：

```bash
cd frontend
pnpm install            # 首次
pnpm dev                # http://localhost:5173
```

浏览器：

| 步骤 | 操作 | 看到 |
|------|------|------|
| 1 | 打开 `http://localhost:5173` | 跳转到 `/login`，AppLayout header 显示 logo + 标题 |
| 2 | 粘贴 `dev-token-change-me` 提交 | 跳到 `/`，TaskList 显示已 seed 的任务（status 排队中） |
| 3 | 等 5 秒 | 列表自动 refetch（DevTools Network 看到 `/api/v1/tasks` 每 5s 一次） |
| 4 | 点任意行 | 跳 `/tasks/<uuid>`，Summary card + 子任务表 |
| 5 | 等任务下载中 | TaskDetail 每 1s 自动刷新；polling 指示器脉动 |
| 6 | 任务 succeeded | polling 自动停止（指示器灰），所有 subtask 标"成功" |
| 7 | 退出登录 | 跳回 `/login` |

---

## 3. MinIO 看真实落地

`http://localhost:9001` 用户/密码 `minioadmin / minioadmin`。

进 `modelpull-dev` bucket，看到 `phase1/<repo_id>/<revision>/<filename>` 路径下的真实文件。点开 `model.safetensors` 看大小（~90MB 整个模型加权重文件之和）。

也可命令行：

```bash
mc alias set local http://localhost:9000 minioadmin minioadmin
mc ls --recursive local/modelpull-dev
mc stat local/modelpull-dev/phase1/sentence-transformers/all-MiniLM-L6-v2/main/model.safetensors
```

---

## 4. 备选路径：本地 Python（不用 Docker）

如果环境没 Docker / 想深入调试单组件：

```bash
# Terminal 1 — controller (PG localhost:5433 trust auth)
uv run alembic upgrade head
DLW_BEARER_TOKEN=dev-token uv run uvicorn dlw.main:app --port 8000

# Terminal 2 — pytest 99 tests + 1 manual smoke skipped
uv run pytest                                  # 99 passed
uv run pytest -m manual --collect-only         # 1 selected (本地有 minio binary 时跑)

# Terminal 3 — frontend
cd frontend && pnpm install && pnpm dev

# Terminal 4 — seed task via curl
TOKEN=dev-token
curl -X POST http://localhost:8000/api/v1/tasks \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"repo_id":"sentence-transformers/all-MiniLM-L6-v2","revision":"main","storage_id":1}'
```

注意：本地路径 executor 不在跑，`task` 会停在 `pending` 状态。要看真下载，要么：
- 单独跑 `uv run dlw-executor`（需配 AWS env + minio 单独装），或
- 走 docker-compose 路径（推荐）

---

## 5. 故障排查

| 现象 | 原因 | 修 |
|------|------|----|
| `docker compose up` 卡住等 minio | 健康检查未通 | `docker compose logs minio` 看；通常端口冲突 |
| controller 启动 502 | PG 没起来 | `docker compose logs postgres`，等 ready 再起 |
| executor `NoSuchBucket` | init-bucket 没成功 | `docker compose logs init-bucket`，重试 `docker compose up -d --force-recreate init-bucket` |
| task 卡 `pending` 永久不动 | executor 没 join 或 poll | `docker compose logs executor` 找 `joining controller` 行 |
| task `failed` with sha mismatch | HF 文件被改 / 环境问题 | 看 `error_message` 字段；通常是网络中断重试用尽 |
| UI 显示 401 | token 不对 / 没设置 | 检查 `localStorage.getItem('dlw_token')` |
| polling 不停 | 任务终态判定问题 | 查 `useTaskDetail.ts` `TERMINAL_STATUSES` 集合 |

---

## 6. Demo 收尾

```bash
docker compose -f docker-compose.dev.yml down -v   # 删 volume，下次干净启
```

UI dev server `Ctrl+C` 即可。
