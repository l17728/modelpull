# modelpull SLA / SLO 基线

> 用途：内部团队部署 / 中小规模 POC 的目标基线。**不构成对外商业 SLA 承诺**
> （那需要先做真实负载压测和事故演练，见 § "为什么是基线而不是承诺"）。
> 更新：2026-05-26。

---

## 1. 服务等级目标（SLO）

| 维度 | SLI（指标） | 目标 SLO | 测量窗口 |
|------|-------------|---------|---------|
| **Controller 可用性** | `/health/ready` 成功率 | **99.5%**（月） | 每分钟探测 |
| **API 延迟（用户面）** | p95 `/api/v1/tasks*` 响应时间 | **< 300 ms** | 滚动 7 天 |
| **AI 助手响应** | p95 `/api/v1/ai/chat` 首字节时间 | **< 3 s** | 滚动 7 天 |
| **任务调度延迟** | 从 `POST /tasks` 到首个 chunk 开始下载 | p95 **< 60 s**（小文件）/ < 300 s（GB 级） | 滚动 7 天 |
| **下载吞吐（每执行器）** | 单 executor 单源 sustained throughput | **≥ 100 MB/s**（取决于网络） | per-task |
| **多源加速比** | 多源 vs 单源对同一任务的 makespan | **≥ 1.5×**（≥ 2 源 enable 时） | per-task |
| **执行器故障恢复 RTO** | active executor 死掉到 standby promote 完成 | **< 30 s** | 故障演练 |
| **数据持久性 RPO** | PG WAL 提交后丢失风险 | **0**（PG 同步落盘） | continuous |
| **审计日志完整性** | 写操作落 `audit_log` 的覆盖率 | **100%**（invariant 16） | 单元测试持续验证 |

---

## 2. 服务等级协议（SLA）— 内部目标

| 等级 | 计划停机窗 | 实际可用性目标 |
|------|-----------|---------------|
| **Critical 路径**（任务创建、运行中任务、配额查询） | 每月 ≤ 30 min 计划维护 | **≥ 99.5%** |
| **AI 助手** | 跟 critical 同维护窗 | **≥ 99.0%**（接受偶发 LLM 后端波动） |
| **报表 / 审计查询** | 可异步降级 | **≥ 99.0%** |
| **多源调度策略调整**（patch_task） | 在线热改，无停机 | **≥ 99.5%** |

**违反 SLA 的事件**：触发 incident response（见 § 4）+ 事后写 post-mortem。

---

## 3. 容量基线（design-based，尚未实测验证）

| 维度 | 设计基线 | 实测验证状态 |
|------|---------|-------------|
| 单 controller 并发 task | ≤ 500 | ❓ 待压测 |
| 单 controller 并发 executor | ≤ 100 | ❓ 待压测 |
| 单 task 子分片数 | ≤ 10000 | ✅ 单测覆盖 |
| 单 task 字节数 | ≤ 5 TB | ✅ chunk 调度算法可扩展 |
| 租户数 | ≤ 1000 | ❓ casbin 在该规模延迟未实测 |
| PG TPS | ≤ 5000 wpps | 见 `docs/perf-baselines/p-005-pg-tps.md`（仅 baseline） |

---

## 4. 事故响应（Incident Response）

### 4.1 严重程度分级

| Sev | 定义 | 响应时间 |
|-----|------|---------|
| **Sev-1** | 全 tenant 无法创建/查询任务；数据丢失风险 | 15 min 内有人响应 |
| **Sev-2** | 部分功能不可用；多 tenant 受影响（如配额误算） | 1 h 内响应 |
| **Sev-3** | 单 tenant 受影响 / AI 助手不可用 / 性能下降 | 工作日内响应 |
| **Sev-4** | 体验问题 / 文档错误 / 单租户单功能 | 下次迭代修复 |

### 4.2 高频故障的处置 runbook

- AI 助手不可用 → [`runbook-ai-assistant.md`](./runbook-ai-assistant.md)
- Local auth 密码遗忘 / bootstrap 失败 → [`runbook-local-auth.md`](./runbook-local-auth.md)
- Executor 大量掉线 → [`executor-runbook.md`](./executor-runbook.md)
- 存储空间不足 → [`storage-reclamation.md`](./storage-reclamation.md)
- 增量下载子分片状态不对 → [`incremental-download.md`](./incremental-download.md)

### 4.3 PostgreSQL 不可用（Sev-1）

```
症状: /health/ready 返回 503，DB 子项 status="failed"
```

1. `psql -h <host> -p 5433 -U postgres -c "SELECT 1"` 确认 PG 实际状态
2. 若 PG 进程死了：标准 PG 恢复流程（systemctl / pg_ctl 重启）
3. 若 PG 起来但 controller 还连不上：检查 `pg_hba.conf` / 防火墙
4. **active controller** 进入 standby 等 PG 恢复（W3c leader-election 自动处理）
5. **缓存的 task**：不丢；scheduler 重启后从 PG 重新 plan

---

## 5. 为什么是"基线"而不是"承诺"

诚实告知：

- ❌ **没跑过真实负载压测** — 上面的 100 MB/s、p95 300ms 都是设计目标，非实测
- ❌ **没在 production 环境跑过 7×24** — 所有验证在 local PG :5433 + 单机
- ❌ **执行器故障恢复 RTO 30s 是单测**，没在多节点真实网络分区下复现
- ❌ **AI 助手 Skills bridge 端到端未对真 opencode 验证**（marker parser 协议假定 LLM 会遵守 manifest，未实测）

把这份基线作为**北极星**，不要作为承诺。要变成对外 SLA，先做：

1. 1-2 周 staging 24h 压测（jmeter / locust）
2. 一次 multi-region 故障演练（断 PG / 断 executor / 断网络）
3. 至少 1 个内部 tenant 跑 30 天有 traffic 的真实数据
4. 把上面的 ❓ 全部变成 ✅ 或修正

---

## 6. 配额（与 SLO 区分）

配额是**租户级容量限制**，不是 SLO。默认值在 `tenants` 表：

| 字段 | 默认 | 含义 |
|------|------|------|
| `quota_bytes_month` | 0（无限） | 月度入站流量上限 |
| `quota_concurrent` | 10 | 同时运行的任务数 |
| `quota_storage_gb` | 1024 | 占用对象存储上限 |
| `quota_ai_tokens_month` | 1,000,000 | 月度 AI Token 用量 |

修改用 `PUT /api/v1/tenants/{id}/quota`（system_admin only，审计记录）— 见
[`runbook-local-auth.md`](./runbook-local-auth.md) §1 类似流程。

---

## 7. 监控 / 告警入口

Prometheus + Grafana 部署在 `deploy/prometheus/` + `deploy/grafana/`。
主要 dashboard：

- `overview-dashboard.json` — 全局健康面板
- `download-throughput.json` — 多源吞吐对比
- `tenant-quota.json` — 配额用量按租户
- 告警规则在 `deploy/alertmanager/`，对应 Sev-1 / Sev-2 上面分级

---

## 8. 变更管理

任何会影响 SLO 的变更（schema migration、controller 配置、leader-election 参
数）必须：

1. 走 PR + CI 全绿
2. 在 staging 验证 ≥ 30 分钟
3. 生产灰度：先单 tenant，再全量
4. 灰度期内 SLI 任何指标降级 → 立即回滚（`alembic downgrade` / git revert）
