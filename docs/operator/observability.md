# 可观测性：Prometheus 指标 + Grafana（部署与使用）

> 用途：让运维/测试人员把 modelpull 的指标接入 Prometheus + Grafana。
> **诚实声明**：管道齐全，但当前代码**只导出 6 个指标**。随仓库附带的
> dashboard / 告警规则是 v2.0 **设计期**产物，引用了约 40 个指标——其中
> 大部分尚未在代码里埋点。本文用「✅ live / 📐 设计期(空面板)」明确区分，
> 别误以为导入 dashboard 就全有数据。

---

## 1. 指标暴露方式

控制器在 `/metrics` 暴露 Prometheus 文本格式（**免认证**，供 scrape；
不要把 `/metrics` 反代到公网——只让内网 Prometheus 抓）。

```bash
# 单机部署里直接看（控制器绑 127.0.0.1:8001）
curl -s http://127.0.0.1:8001/metrics | grep '^dlw_'
```

## 2. 当前真正导出的指标（✅ live，共 12 个）

定义集中在 `src/dlw/observability/metrics.py`，埋点在对应 service 里。

**核心运维信号**（任务在跑吗 / worker 活着吗 — 撑起 overview 顶部健康面板）：

| 指标 | 类型 | 含义 | 埋点位置 |
|------|------|------|----------|
| `dlw_controller_role{role}` | Gauge | leader 角色 one-hot（active/standby/recovering） | leader 循环 state hook |
| `dlw_tasks_active_count{tenant_id}` | Gauge | 非终态任务数（按租户），sweep 循环每 30s 刷新 | active controller sweep |
| `dlw_tasks_completed_total{status}` | Counter | 任务终态计数（succeeded/failed/cancelled） | scheduler + recovery 终态 |
| `dlw_task_duration_seconds` | Histogram | 任务创建→终态耗时 | 同上 |
| `dlw_executors_count` | Gauge | 在线（非 faulty）executor 数 | active controller sweep |
| `dlw_executor_status{executor_id,status}` | Gauge | 单 executor 健康 one-hot | state_machine 状态转移 + 注册 |
| `dlw_subtask_retries_total` | Counter | 子任务重试/重排次数 | reclaim + 启动恢复 |

**v2.1 特性指标**：

| 指标 | 类型 | 含义 | 有数据的 dashboard |
|------|------|------|--------------------|
| `dlw_replication_bytes_total{tenant_id,target_storage_id,status}` | Counter | 跨地域复制传输字节 | replication-throughput |
| `dlw_replication_jobs_total{tenant_id,status}` | Counter | 复制任务终态计数 | replication-throughput |
| `dlw_replication_job_duration_seconds{status}` | Histogram | 单复制任务耗时 | replication-throughput |
| `dlw_optimizer_solve_duration_seconds` | Histogram | optimizer.solve() 耗时 | optimizer-dashboard（部分） |
| `dlw_replan_chunk_moves_total{mode}` | Counter | replan 移动的 chunk 数（shadow/apply） | optimizer-dashboard（部分） |

> 加上 prometheus_client 自带的 `process_*` / `python_gc_*` 进程指标。

## 3. 部署 Prometheus + Grafana

### 3.A 单机 docker（与现有 compose 同机）

控制器绑 loopback，所以 Prometheus 要能进 compose 的 docker 网络。最简单
是把 Prometheus + Grafana 作为额外 compose 服务加到同一 network，scrape
`controller:8001`：

```yaml
# 追加到 deploy/single-host/docker-compose.yml 的 services: 下
  prometheus:
    image: prom/prometheus:latest
    container_name: dlw-prometheus
    restart: unless-stopped
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - ../prometheus/recording-rules.yaml:/etc/prometheus/recording-rules.yaml:ro
      - ../prometheus/alerting-rules.yaml:/etc/prometheus/alerting-rules.yaml:ro
      - dlw-promdata:/prometheus
    ports: ["127.0.0.1:9090:9090"]   # loopback；反代或 SSH 隧道访问
  grafana:
    image: grafana/grafana:latest
    container_name: dlw-grafana
    restart: unless-stopped
    environment:
      GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_ADMIN_PASSWORD:-admin}
    volumes: [dlw-grafanadata:/var/lib/grafana]
    ports: ["127.0.0.1:3000:3000"]
# volumes: 段追加 dlw-promdata: 和 dlw-grafanadata:
```

`deploy/single-host/prometheus.yml`（新建）：

```yaml
global:
  scrape_interval: 30s
rule_files:
  - /etc/prometheus/recording-rules.yaml
  - /etc/prometheus/alerting-rules.yaml
scrape_configs:
  - job_name: dlw-controller
    metrics_path: /metrics
    static_configs:
      - targets: ["controller:8001"]   # docker DNS，同 network
```

起：`docker compose up -d prometheus grafana`。Grafana 在 `127.0.0.1:3000`
（反代或 SSH 隧道访问），加 Prometheus 数据源 `http://dlw-prometheus:9090`。

### 3.B Kubernetes（Helm + Prometheus Operator）

Helm chart 已带 `ServiceMonitor` + `PodMonitor`（`deploy/helm/templates/servicemonitor.yaml`），
默认关闭。打开：

```yaml
# values.yaml
observability:
  prometheus:
    serviceMonitor:
      enabled: true
      interval: 30s
```

`helm upgrade dlw deploy/helm/ -f values.yaml`。Prometheus Operator 会自动
发现并 scrape 控制器 Service 的 `http` 端口 `/metrics` + 各 executor pod 的
`metrics` 端口。

## 4. 导入 Grafana dashboard

`deploy/grafana/` 下 6 个 JSON，Grafana → Dashboards → Import → Upload JSON：

| 文件 | 数据现状 |
|------|----------|
| `replication-throughput.json` | ✅ **live**（replication 三个指标都已埋点） |
| `overview-dashboard.json` | 🟢 **大部分 live**：controller_role / 活动任务数 / 完成数 / 任务时长 / executor 状态 + 在线数都已埋点；剩 subtask_state、executor_health_score、audit_chain 等 📐 空 |
| `optimizer-dashboard.json` | 🟡 部分：solve duration + replan moves live，其余决策类指标 📐 空 |
| `slo-dashboard.json` | 📐 设计期：SLI/SLO burn-rate 指标未埋点，全空 |
| `ai-copilot-dashboard.json` | 📐 设计期：AI token/cost/tool 指标未埋点，全空 |
| `enterprise-network-dashboard.json` | 📐 设计期：反向 WSS / 凭证 / 限速指标未埋点，全空 |

## 5. ⚠️ 已知差距：dashboard/告警规则 ≫ 已埋点指标

`deploy/grafana/*.json` 引用约 **42** 个指标、`deploy/prometheus/*.yaml`
引用约 **38** 个，而代码导出第 2 节那 **12** 个（核心 task/executor 信号 +
v2.1 特性指标）。仍有约 30 个**未埋点**——它们是被有意判定为「非必要、暂不
实现」的：AI 成本/token（助手便利功能）、企业内网限速/console 细节（实验性）、
SLO burn-rate recording rules（需成熟延迟 SLI，过早）、optimizer 细粒度决策
（flag-gated 实验）、磁盘/源/证书二级指标。后果：

- 多数 dashboard 面板显示「No data」
- 多数告警规则（`alerting-rules.yaml`）永不触发——**别据此误判系统健康**
- recording rules 里依赖未埋点指标的派生序列同样为空

要让它们变实，需在对应 service 里加 prometheus_client 埋点（参考第 2 节
6 个已埋点指标的写法：在 `observability/metrics.py` 定义句柄、在 service
里 `.inc()/.observe()/.set()`）。这是一项独立的 instrumentation 工作，不在
当前部署范围内。

## 6. 健康检查（不依赖指标，始终可用）

指标缺口不影响存活探针——这些端点**实时反映真实状态**：

```bash
curl -s http://127.0.0.1:8001/health/live    # 进程存活
curl -s http://127.0.0.1:8001/health/ready   # 含 DB 连通
curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8001/health/active  # 200=active leader, 503=standby
```

排查日志见 [`deploy/single-host/README.md`](../../deploy/single-host/README.md) §Logs。
