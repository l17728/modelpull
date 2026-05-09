# modelpull — Phase 1 Alpha Pitch

> 5-minute talking points for internal demo. Audience: 队内同事 / leader / 协作部门。

---

## 1. Problem (60 秒)

我们日常拉模型权重碰到三个具体疼点：

| 模型 | 体量 | 单机直拉痛感 |
|------|------|--------------|
| DeepSeek-V3 (FP8) | 689 GB / 163 文件 | 百兆带宽 8-24h，断网从头来 |
| Kimi-K2-Instruct (FP8) | 1030 GB / 61 文件 | 单磁盘装不下，单进程超时 |
| Qwen3-72B-Instruct (BF16) | 144 GB / 30 文件 | 国内 HF 直连不可用，要走 mirror |

现状：每个工程师各自 `huggingface-cli download`，挂了从头，磁盘占满，缓存重复，没有统一存储。

> "如果 8 个人同时拉同一个模型，是 8 倍带宽 + 8 倍磁盘 + 8 次失败重做。"

---

## 2. Solution (90 秒)

modelpull 是一个**控制器 + 执行器 + S3 存储**的分布式权重下载系统。一句话价值：

> **任务交给 controller，executor 流式拉到统一 S3，浏览器看进度，sha256 自动校验。失败了重试在 controller 一侧，不用从头重做。**

架构（Phase 1 alpha）：

```
                                            ┌─────────────┐
                                            │  HuggingFace │
                                            │     Hub      │
                                            └──────┬──────┘
                                                   │ httpx async stream
   ┌──────────┐  POST /tasks                       │
   │ Browser  │ ────────────┐                      ▼
   │ (Vue 3)  │             │              ┌──────────────┐  S3 multipart
   └────┬─────┘             │              │   Executor   │ ─────────────┐
        │ 1s polling        │              │ (Python CLI) │              │
   ┌────▼─────────────┐     │              └──────┬───────┘              ▼
   │   Controller     │  ◄──┘                     │ POST /poll       ┌──────┐
   │  FastAPI + PG    │ ────────────────────────► │ /heartbeat       │ S3 / │
   │ (HF metadata,    │  scheduler.claim_one_     │ /report          │MinIO │
   │  scheduler,      │  subtask FOR UPDATE       │                  │      │
   │  sha256 verify)  │  SKIP LOCKED                                  └──────┘
   └──────────────────┘
```

**关键不变量**（架构上保证，不是事后兜底）：

1. 单流：HF GET → 同字节流 → sha256 累计 + S3 multipart parts 上传，**内存 O(5MB)**，零落盘。
2. sha256 是 **controller 端校验**，不是 executor 自报 — 单一可信 referee。
3. `FOR UPDATE SKIP LOCKED` 调度：N 个 executor 并发 poll 不会拿到同一个 subtask。
4. 任务终态在 `complete_subtask` 内原子转换：所有 subtask succeeded → task succeeded；任一 failed → task failed。
5. FK `ondelete=CASCADE` 兜底 subtask 删除；ORM 不做 `delete-orphan` 避免 lazy-load 误删。

---

## 3. Demo flow (3 分钟)

按 [`runbook.md`](./runbook.md) 走，关键 3 个画面：

**Frame 1 — 一键起栈**

```
$ ./scripts/demo-alpha.sh
[1/5] Boot stack ...                    ← docker-compose 5 服务
[2/5] Wait for controller /health/ready
      controller ready
[3/5] POST /api/v1/tasks ...
      task_id = 27eb6723…
[4/5] Poll until terminal state ...
      [19:54:12] status: pending
      [19:54:14] status: scheduling
      [19:54:18] status: downloading
      [19:55:43] status: succeeded
[5/5] Summary:
      ✓ succeeded   8/8
      MinIO console: http://localhost:9001
```

**Frame 2 — UI 实时进度**（截图：`docs/demo/screens/03-task-detail-active.png`）

打开 `http://localhost:5173`，粘 token，点任务行：

- 顶部 summary card：repo / revision / status badge / created_at
- 下方 subtasks 表：8 个文件，单个进度
- 状态实时变化：每秒一次刷新；任务进 succeeded 后**自动停止轮询**（不烧后端）

**Frame 3 — MinIO 看真实文件**

`http://localhost:9001` → bucket `modelpull-dev` → `phase1/sentence-transformers/all-MiniLM-L6-v2/main/`：

- `config.json` 612 B
- `model.safetensors` 90.9 MB ← 真模型权重，可下载，sha256 已与 HF 公布值校验过

> "这不是 mock。是真 HF 拉下来再传 S3。把这个 bucket 挂到下游模型服务直接用。"

---

## 4. 已完成 vs 推迟（30 秒）

✅ Phase 1 已落地 Phase 1 §1.5 全部出场标准（5 个 PR / 99 backend tests + 18 frontend tests / CI 12/12）：

- 单租户 PoC：UI 任务创建 → controller 调度 → executor 下载 → S3
- 真 HF Hub API（`huggingface_hub` SDK 文件枚举 + httpx 流式下载）
- 任务级最终校验（sha256 比对 expected vs actual）
- alembic schema migration 支持

⏸️ 显式推迟到 Phase 2/3（spec 都写好了）：

- mTLS executor 认证 / fence-token / executor_epoch
- 多租户 RBAC + OIDC
- HF Token reverse-proxy（Phase 1 简化为 executor env）
- 多 executor 协调 + crash recovery + multipart 续传
- 多源（HF + ModelScope + hf-mirror）+ chunk-level 多线程

---

## 5. 数据 (30 秒)

| 指标 | 值 |
|------|----|
| 实施周数 | 5 个 PR / ~5 周（plan 计划 6 周） |
| Backend Python LOC | 2138 行 |
| Frontend (TS + Vue) LOC | 823 行 |
| Test LOC | 3143 行 |
| Backend tests | 99 passing + 1 manual smoke deselected |
| Frontend tests | 18 Vitest passing |
| CI checks | 12 jobs × 5 PR = 60 个 check 一次性绿 4 / 5 次（Week 2 是 2 轮，其余一次过） |
| Pre-execution review pattern | 4 plan × 9-11 plan-level 修订 = 0 ~ 1 轮 CI 迭代 |

---

## 6. Q & A 准备

**Q：为啥不用 huggingface_hub.snapshot_download？**
A：那个本地落盘 → 再 boto3 upload。我们要单流不落盘 + 同字节计算 sha256。本地磁盘对 18.5GB 的 GLM 不友好。

**Q：私有模型怎么办？**
A：Phase 1 简化为 controller env `DLW_HF_TOKEN`。Phase 2 上 reverse-proxy（不变量 2：HF token 不离开 controller）。

**Q：分布式安全？多 executor？**
A：Phase 1 单 executor，明文不变量 1-9 已实现，10+（fence-token / mTLS / 多 executor 协调）是 Phase 2 三周计划，spec 已写。

**Q：和阿里云 PAI / Modelhub 内部工具区别？**
A：modelpull 自己带调度、UI、CI、可观测性，不依赖云厂商；自托管 alpha 即可跑 demo；后期可作为自有平台底座或被云厂商工具集成。

**Q：跑过 18.5GB GLM-4-9B 吗？**
A：Phase 1 §1.5 出场闸只要"能完成 1 个模型 HF→S3 端到端 sha256 校验"。我们用 90MB 公开小模型（`all-MiniLM-L6-v2`）通过；架构上无 18GB 限制（流式 O(5MB) 内存），但真跑大模型 walltime + 网络稳定性是 Phase 2 chunk-level 多线程下载的事。
