# 07 — 测试计划

> 角色：QA / 后端 / SRE 制定与执行测试。每个测试都标记**优先级**与**所属 phase**（详见 08）。
> 范围：单元 / 集成 / E2E / 性能 / 安全 / 合规 / Chaos。
> 目标覆盖率：单元 ≥ 80% (line) / 关键路径 100% (branch)。

---

## 0. 测试金字塔与工具栈

```
              ╱╲
             ╱  ╲    Chaos / GameDay (季度)
            ╱────╲
           ╱      ╲   Performance / Stress (~10 基线)
          ╱────────╲
         ╱          ╲  E2E (~30，CI nightly)
        ╱────────────╲
       ╱              ╲ Integration (~80，CI per-PR)
      ╱────────────────╲
     ╱                  ╲ Unit (~300，CI per-commit)
    ╱────────────────────╲
```

| 层 | 工具 | 运行时机 |
|----|------|---------|
| Unit | `pytest` + `pytest-asyncio` + `hypothesis`（属性测试） | per-commit (≤ 30s) |
| Integration | `pytest` + `testcontainers` (PG/MinIO/wiremock) | per-PR (≤ 5 min) |
| E2E | `pytest` + 真容器栈（docker-compose） | nightly (≤ 30 min) |
| Performance | `locust` / `k6` | weekly + 发布前 |
| Security | OWASP ZAP / Trivy / `bandit` / `gitleaks` / `pip-audit` | per-PR |
| Chaos | chaos-mesh / litmus | 季度 |
| Frontend | Playwright | per-PR |

---

## 1. 单元测试矩阵（~300）

### 1.1 状态机（~50）

> 不变量索引 5, 6, 7（详见 01 §7）

| ID | 模块 | 测试 | Phase |
|----|------|------|-------|
| U-SM-001 | task_state_machine | 所有合法 transition 都成功 | 1 |
| U-SM-002 | task_state_machine | 所有非法 transition 抛 `IllegalTransition` | 1 |
| U-SM-003 | task_state_machine | terminal 状态不能跃迁 | 1 |
| U-SM-004 | task_state_machine | `cancelling` → `cancelled` 仅当所有 in-flight 完成 | 2 |
| U-SM-005 | subtask_state_machine | `pending → assigned` 必须有 assignment_token | 2 |
| U-SM-006 | subtask_state_machine | `paused_external` 不计入 retry_count | 2 |
| U-SM-007 | subtask_state_machine | `paused_disk_full` 阻断后续分配 | 2 |
| U-SM-008 | executor_state_machine | degraded ↔ suspect 死循环不再发生（D3 修复） | 2 |
| U-SM-009 | executor_state_machine | suspect → degraded 时 `consecutive_heartbeat_failures` 清零 | 2 |
| U-SM-010 | executor_state_machine | probationary canary 通过 N=3 才升 healthy | 2 |
| U-SM-011 | executor_state_machine | 每次 transition 写 `executor_status_history` | 2 |
| U-SM-012 | state_machine_yaml | YAML 中的 transitions 与代码实现一致（CI 断言） | 1 |

(同样模式覆盖 5 类状态机，约 50 个测试)

### 1.2 调度与 Fence Token（~40）

| ID | 测试 | 关键不变量 |
|----|------|----------|
| U-SCHED-001 | `assign_subtask` CAS 成功路径返回新 token | 不变量 6 |
| U-SCHED-002 | CAS 失败时不进 enqueue | 不变量 6 |
| U-SCHED-003 | stale executor epoch → `StaleExecutor` raise | 不变量 9 |
| U-SCHED-004 | `complete_subtask` 用错 token → `STALE_ASSIGNMENT` | 不变量 6 |
| U-SCHED-005 | `complete_subtask` 错误 epoch → `STALE_ASSIGNMENT` | 不变量 9 |
| U-SCHED-006 | `reclaim_subtasks` 仅清理匹配 epoch 的 subtask | 不变量 9 |
| U-SCHED-007 | reclaim 后新 register 不被旧 reclaim 影响 | D6 修复 |
| U-SCHED-008 | `pick_candidate_subtask` 用 SKIP LOCKED | 并发 |
| U-SCHED-009 | LPT 算法：files=[10G,5G,3G,1G] sources={A:2x, B:1x} → A 拿 [10G], B 拿 [5G,3G,1G] | 06 §1.6 |
| U-SCHED-010 | 同 host_id 下不同 executor 不会拿同文件不同 chunk | 不变量 10 |
| U-SCHED-011 | 优先级抢占仅 priority=3 触发 | 03 §4.3 |
| U-SCHED-012 | 多 candidate 时优先 storage region affinity | 02 §3 |

### 1.3 校验链路（~30）

| ID | 测试 |
|----|------|
| U-VER-001 | 单线程流式 SHA256 与 hashlib 全文件 SHA256 一致 |
| U-VER-002 | 多线程 chunk 模式：完成后必须二次扫描 |
| U-VER-003 | 任务级最终校验比对 `expected == actual` for ALL subtasks（不变量 5） |
| U-VER-004 | 仅 size 匹配但 sha 不匹配 → 任务 failed |
| U-VER-005 | 远端 ChecksumSHA256 解析正确（base64 → hex） |
| U-VER-006 | 多源 chunk 完成后用 HF sha 验证（不变量 11） |

### 1.4 多源（~50）

| ID | 测试 | 文档 |
|----|------|------|
| U-SRC-001 | NameResolver identity 80% 命中（deepseek-ai/X） | 06 §1.5 |
| U-SRC-002 | NameResolver 规则映射 meta-llama → LLM-Research | 06 §1.5 |
| U-SRC-003 | NameResolver API 反查缓存 24h | 06 §1.5 |
| U-SRC-004 | LPT 启发式：3 文件 4 源最优分配 | 06 §1.6 |
| U-SRC-005 | LPT corner: 单文件 + 多源 → chunk-level | 06 §1.6 |
| U-SRC-006 | LPT corner: 单源单文件 → degenerate to single | 06 §1.6 |
| U-SRC-007 | 测速并发 N×M 不阻塞 | 06 §1.8 |
| U-SRC-008 | 测速软超时：deadline 到了用已收字节算速度 | 06 §1.8 |
| U-SRC-009 | EWMA 融合：实测 0.7 + 历史 0.3 | 06 §1.8 |
| U-SRC-010 | `_solve_optimal_combination`：第 4 个慢源不会被加入 | 06 §1.8 |
| U-SRC-011 | 协调开销惩罚 2%/源 | 06 §1.8 |
| U-SRC-012 | 5xx 连续 3 次拉黑 5min | 06 §1.7 |
| U-SRC-013 | 拉黑指数退避到 30min 上限 | 06 §1.7 |
| U-SRC-014 | sha 不匹配拉黑 24h | 不变量 12 |
| U-SRC-015 | HF 失效但其他源活 → paused（除非 trust_non_hf） | 不变量 13 |
| U-SRC-016 | HF Mirror 不带 token，gated 模型自动跳过此源 | 06 §1.9.2 |

(覆盖 6 个内置驱动 × 平均 8 个测试 ≈ 48)

### 1.5 安全 / 输入校验（~40）

| ID | 测试 |
|----|------|
| U-SEC-001 | `validate_repo_id` 拒绝 `../../etc/passwd` |
| U-SEC-002 | `validate_repo_id` 拒绝 `aaa//bbb` |
| U-SEC-003 | `validate_repo_id` 接受合法 `org/model-name_v2` |
| U-SEC-004 | `validate_revision` 拒绝 `main`/`master` |
| U-SEC-005 | `validate_revision` 接受 40-char hex |
| U-SEC-006 | `safe_filename` 拒绝 `../foo` |
| U-SEC-007 | `safe_filename` 拒绝符号链接逃逸 |
| U-SEC-008 | `safe_hf_get` 拒绝非白名单域名 |
| U-SEC-009 | `safe_hf_get` 拒绝 http:// |
| U-SEC-010 | redactor 替换 `hf_xxx` → `[HF_TOKEN]` |
| U-SEC-011 | redactor 替换 `AKIA...` |
| U-SEC-012 | redactor 替换 `Authorization: Bearer ...` |
| U-SEC-013 | envelope encryption 解密能还原明文 |
| U-SEC-014 | envelope encryption KEK 不同时解密失败 |
| U-SEC-015 | HMAC 心跳：篡改 body 后校验失败 |
| U-SEC-016 | HMAC 心跳：nonce 重放检测 |
| U-SEC-017 | clock skew > 60s 拒绝 |
| U-SEC-018 | JWT 过期拒绝 |
| U-SEC-019 | Pickle classification: `*.bin` → DANGEROUS |
| U-SEC-020 | safetensors classification: SAFE |
| U-SEC-021 | `*.py` → CODE_EXECUTION |
| U-SEC-022 | License policy: deny → 任务拒绝 |
| U-SEC-023 | License policy: warn → 任务带 warning 标记 |
| U-SEC-024 | gated 模型未审批 → 任务进 `pending_approval` |

### 1.6 数据模型与 DB（~50）

| ID | 测试 |
|----|------|
| U-DB-001 | `tenants.slug` UNIQUE 约束 |
| U-DB-002 | `download_tasks` UNIQUE (tenant_id, repo_id, revision) when not failed |
| U-DB-003 | `file_subtasks` UNIQUE (task_id, filename) |
| U-DB-004 | `executors.epoch` 单调递增（每次 register +1） |
| U-DB-005 | 业务表都有 `tenant_id` （information_schema 扫描）（不变量 8） |
| U-DB-006 | `audit_log.self_hash` 链式哈希正确 |
| U-DB-007 | `audit_log` 篡改单行后链断 |
| U-DB-008 | usage_records append-only（DELETE 拒绝） |
| U-DB-009 | quota_snapshots 1 分钟 cron 重算正确 |
| U-DB-010 | 任务 cancel 删除 → subtask CASCADE 删除 |
| U-DB-011 | storage_object refcount 减到 0 后 GC 标记 |

### 1.7 消息序列化与协议（~30）

| ID | 测试 |
|----|------|
| U-PROTO-001 | 心跳 body schema 严格校验（pydantic） |
| U-PROTO-002 | WS snapshot/delta seq 单调递增 |
| U-PROTO-003 | WS resync：last_seq 在 buffer 内续推 |
| U-PROTO-004 | WS resync：last_seq 太老 → snapshot |
| U-PROTO-005 | OpenAPI 3.1 schema 与 pydantic models 一致（CI） |

### 1.8 工具函数（~30）

CSV-like 累计：路径、时间、字节解析、ID 生成等。

---

## 2. 集成测试矩阵（~80）

> 用 testcontainers 起真 PG + 真 MinIO + wiremock HF。

### 2.1 Controller × DB（~15）

| ID | 测试 |
|----|------|
| I-DB-001 | recovery_routine：`uploading` 远端不存在 → 回 pending |
| I-DB-002 | recovery_routine：`uploading` 远端 sha 不匹配 → 删除 + 回 pending |
| I-DB-003 | recovery_routine：`uploading` 远端三联校验通过 → verified |
| I-DB-004 | recovery_routine：清理 24h 前 multipart upload_id |
| I-DB-005 | recovery_routine：HF 全局 throttle 状态从 DB 加载 |
| I-DB-006 | quota_snapshots cron：1min 重算 |
| I-DB-007 | active/standby：advisory_lock 切换 |
| I-DB-008 | schema migration：alembic upgrade head 成功 + 回滚 |
| I-DB-009 | PG 连接池耗尽时优雅排队 |

### 2.2 Controller × Executor（~20）

| ID | 测试 |
|----|------|
| I-CE-001 | Register 全流程：CSR → 颁发 mTLS 证书 + JWT |
| I-CE-002 | Register 后 epoch 递增 |
| I-CE-003 | 心跳成功：进度持久化 |
| I-CE-004 | 心跳带过期 epoch → 401 EPOCH_MISMATCH |
| I-CE-005 | 心跳被篡改（HMAC） → 401 |
| I-CE-006 | 心跳重放 nonce → 409 NONCE_REPLAY |
| I-CE-007 | 心跳 timestamp skew > 60s → 409 CLOCK_SKEW |
| I-CE-008 | 心跳响应中带 new assignment + token |
| I-CE-009 | Subtask complete 用错 token → 409 STALE_ASSIGNMENT |
| I-CE-010 | Subtask complete 用对 token → 200 + status verified |
| I-CE-011 | Heartbeat timeout 3 次 → suspect → 6 次 → faulty + reclaim |
| I-CE-012 | 重 register 后老 reclaim 不影响新 assignment（D6） |
| I-CE-013 | mTLS 证书过期前自动续签 |

### 2.3 Controller × Storage（~10）

| ID | 测试 |
|----|------|
| I-CS-001 | S3 multipart upload 完整流程 |
| I-CS-002 | multipart 中断 → recovery abort |
| I-CS-003 | STS 临时凭证签发与 boto3 续期 |
| I-CS-004 | ChecksumSHA256 校验 |
| I-CS-005 | 同 sha 二次上传：去重生效 (refcount++) |
| I-CS-006 | refcount 减到 0 → GC 标记 |
| I-CS-007 | 90 天未访问 → archive 移动 |

### 2.4 Controller × HF Reverse Proxy（~10）

| ID | 测试 |
|----|------|
| I-HFP-001 | Executor → controller proxy → HF（mock）→ 透传 bytes |
| I-HFP-002 | proxy 不在落盘（流式） |
| I-HFP-003 | HF Token 注入 Authorization header |
| I-HFP-004 | 用错 (subtask, token, epoch) → 409 |
| I-HFP-005 | HF 返回 302 CDN 重定向自动 follow |
| I-HFP-006 | HF 返回 429 → controller 全局 throttle 状态机更新 |

### 2.5 多源驱动 × Source API mock（~25）

每个驱动至少 4 个测试（resolve / download / health / failure）：

| ID | 驱动 | 测试 |
|----|------|------|
| I-SRC-HF-* | huggingface | resolve / range download / 401 / CDN follow |
| I-SRC-MIRROR-* | hf_mirror | gated 模型 fallback / public 访问 |
| I-SRC-MS-* | modelscope | NameResolver 调用 / 中文 LLM 下载 |
| I-SRC-WM-* | wisemodel | resolve / download / 不可达兜底 |
| I-SRC-OCSG-* | opencsg | resolve / download |
| I-SRC-S3M-* | s3_mirror | sha 抽样校验 / IRSA 凭证 |

---

## 3. E2E 测试（~30）

> docker-compose 全量起：controller + 2 executor + PG + MinIO + 2 mock sources。

### 3.1 核心 Happy Path（~5）

| ID | 场景 |
|----|------|
| E2E-001 | 创建任务 → 单源完成 689GB（mock，加速因子 1000x） |
| E2E-002 | 创建任务 → 多源 auto_balance 完成 |
| E2E-003 | 创建任务 → 增量升级（upgrade_from_revision） |
| E2E-004 | 任务取消 → cancelled，verified 文件保留 |
| E2E-005 | 大文件 chunk-level 分给 2 个源 + 二次扫描通过 |

### 3.2 故障注入（~10）

| ID | 注入 | 期望 |
|----|------|------|
| E2E-FI-001 | 杀单 executor 中途 → 另一 executor 接管 |
| E2E-FI-002 | 杀 controller → standby 提升 → 任务恢复 |
| E2E-FI-003 | HF mock 全 429 → paused_external + 30min 后重试 |
| E2E-FI-004 | S3 mock 5xx → 重试 → 切 fallback storage |
| E2E-FI-005 | Executor 断网 60s → reclaim → 网恢复后继续 |
| E2E-FI-006 | 删 executor `.parts/` → resume 重下 |
| E2E-FI-007 | PG primary down → standby 提升 |
| E2E-FI-008 | 多源中 1 个测速失败 → 仅用其他源 |
| E2E-FI-009 | sha 不匹配 → 源拉黑 24h → 切 HF |
| E2E-FI-010 | 磁盘满 → paused_disk_full → 扩容后恢复 |

### 3.3 多租户隔离（~5）

| ID | 测试 |
|----|------|
| E2E-MT-001 | tenant A 看不到 tenant B 任务 |
| E2E-MT-002 | tenant A 配额满 → 创建任务 429 |
| E2E-MT-003 | tenant A 用 tenant B 的 HF token？拒绝 |
| E2E-MT-004 | tenant A 的存储凭证 tenant B 不可访问 |
| E2E-MT-005 | RBAC：viewer 角色不能 cancel |

### 3.4 安全 / 合规（~5）

| ID | 测试 |
|----|------|
| E2E-SEC-001 | 未认证 API 全部 401 |
| E2E-SEC-002 | 错误 Origin WS 拒绝 |
| E2E-SEC-003 | gated 模型走审批工作流 |
| E2E-SEC-004 | 审计日志：每个 admin 操作有记录 |
| E2E-SEC-005 | 审计链篡改单行：tampering 检测 |

### 3.5 升级 / 灰度（~5）

| ID | 测试 |
|----|------|
| E2E-UP-001 | Executor 滚动升级：任务不中断 |
| E2E-UP-002 | Controller standby 升级后切换 |
| E2E-UP-003 | DB schema migration：可升可降 |
| E2E-UP-004 | v1.x 数据导入到 v2.0 后任务可继续 |
| E2E-UP-005 | active/standby 重启后 advisory_lock 正确切换 |

---

## 4. 性能 / 压力测试（~10 基线）

### 4.1 容量基线

| ID | 场景 | 目标 SLI | 工具 |
|----|------|---------|------|
| P-001 | Controller 心跳处理 | 5000 ops/s 持续 10min | k6 |
| P-002 | Controller API QPS | 2000 r/s, P99 < 200ms | locust |
| P-003 | WS broadcast | 200 connections, 100KB/s/connection | k6 ws |
| P-004 | 单 executor 下载 | 1GB/s 持续 5min（NIC=10Gbps） | iperf 对比 |
| P-005 | PG TPS | 5000 commits/s | pgbench |

### 4.2 负载形态

| ID | 场景 | 期望 |
|----|------|------|
| P-006 | 突发：10 任务同时创建 | 调度无饥饿，5min 内全部 scheduling |
| P-007 | 持续：100 并发任务 24h | 无内存泄漏，错误率 < 0.1% |
| P-008 | Soak：50 并发任务 1 周 | 稳态行为 |

### 4.3 退化检测

| ID | 场景 |
|----|------|
| P-009 | 新版本 vs 上版本：吞吐回归 < 5% |
| P-010 | 加 100 个 metrics label 不影响 P99 < 200ms |

---

## 5. 安全测试（~20）

### 5.1 自动化扫描（CI per-PR）

| ID | 工具 | 配置 |
|----|------|------|
| S-001 | `bandit` | 全代码静态扫 |
| S-002 | `pip-audit` | 依赖漏洞 |
| S-003 | `Trivy` | Docker 镜像扫描 |
| S-004 | `gitleaks` | secret 泄漏扫描 |
| S-005 | `OWASP ZAP` baseline | 部署后跑被动扫描 |
| S-006 | `sqlmap` 关键 endpoint | SQL 注入 |

### 5.2 渗透测试用例（手动 / 半自动）

| ID | 类别 | 测试 |
|----|------|------|
| S-PEN-001 | OWASP A1 注入 | repo_id 含 `'; DROP TABLE`：被 ORM 参数化挡住 |
| S-PEN-002 | OWASP A1 注入 | path traversal 在 filename / path_template |
| S-PEN-003 | OWASP A2 失效认证 | JWT 过期后访问拒绝 |
| S-PEN-004 | OWASP A2 失效认证 | mTLS 证书撤销 |
| S-PEN-005 | OWASP A3 敏感信息 | 错误响应不泄漏 stack trace |
| S-PEN-006 | OWASP A3 敏感信息 | 日志中无 token / AK 明文（gitleaks） |
| S-PEN-007 | OWASP A4 XXE | （N/A，不解析 XML） |
| S-PEN-008 | OWASP A5 配置错误 | DEBUG mode 不在生产 |
| S-PEN-009 | OWASP A6 漏洞组件 | pip-audit 无 high/critical |
| S-PEN-010 | OWASP A7 XSS | UI 渲染 executor 字符串：`<script>` 不执行 |
| S-PEN-011 | OWASP A8 反序列化 | 不接受 pickle 上传 |
| S-PEN-012 | OWASP A9 Logging | 所有 admin 操作有审计 |
| S-PEN-013 | OWASP A10 SSRF | repo_id `?url=http://localhost/admin` 被拒 |
| S-PEN-014 | DoS | 无认证调 `/api/tasks` 1000 次：rate limited |

### 5.3 合规测试（~5）

| ID | 标准 | 测试 |
|----|------|------|
| C-001 | SOC2 CC7.2 | 审计链每条 self_hash 一致 |
| C-002 | SOC2 CC7.2 | 审计日志导出到 WORM |
| C-003 | SOC2 CC6.1 | RBAC：未授权访问拒绝 |
| C-004 | ISO 27001 A.12.4 | 日志保留 ≥ 365 天 |
| C-005 | License 合规 | gated 模型必须审批 |

---

## 6. Chaos / GameDay 演练

详见 05 §9。每季度执行一次：

| 演练 ID | 场景 | RTO | 验证 |
|--------|------|-----|------|
| CH-Q1 | 杀 controller-active | ≤ 10min | RB-01 流程跑通 |
| CH-Q2 | HF 全网 429 | N/A | paused_external 自动恢复 |
| CH-Q3 | PG primary 故障 | ≤ 5min | streaming replica 提升 |
| CH-Q4 | 网络分区 | N/A | reclaim 不丢数据 |

---

## 7. CI/CD 集成

### 7.1 阶段

```
Pre-commit hook:
  - black / ruff
  - mypy strict
  - pytest -m unit (≤30s)

PR check (GitHub Actions / GitLab CI):
  - All unit + integration (≤5min)
  - Security scans (bandit/pip-audit/gitleaks)
  - Coverage gate ≥ 80%
  - OpenAPI schema diff（破坏性变更需手动批准）

Merge to main:
  - Build + push docker images
  - Run E2E nightly suite
  - Deploy to staging

Tag release:
  - Performance regression tests
  - Run full E2E + chaos
  - Deploy to prod (after manual approval)
```

### 7.2 覆盖率要求

| 类别 | 目标 |
|------|------|
| 行覆盖 | ≥ 80% |
| 分支覆盖（关键路径：state machine, fence, recovery） | 100% |
| Mutation testing（`mutmut`） | ≥ 70% |

### 7.3 测试数据管理

- **种子模型**：mock HF 用 `test/tiny-model`（小 LFS 文件 + .json + .py）
- **大文件 mock**：用 `seek` + `truncate` 创建稀疏文件，sha256 已知
- **多源 mock**：wiremock + 可注入 5xx / sha 不匹配的 plugin

---

## 8. 测试与 Phase 的对应

| Phase | 必通过的测试集 |
|-------|---------------|
| 1 (4w) | U-SM-001..012, U-DB-*, U-VER-001..003, I-CE-001..010, E2E-001 |
| 2 (3w) | + U-SCHED-*, U-SEC-001..010, I-CE-011..013, I-DB-*, E2E-FI-001..006 |
| 3 (3w) | + U-SRC-*, I-SRC-*, E2E-MT-*, E2E-002..003, P-001..005 |
| 4 (3w) | + U-SEC-*, I-CS-*, S-PEN-*, E2E-SEC-*, E2E-UP-*, P-006..010, CH-Q1..Q4 |

详见 [08-mvp-roadmap.md](./08-mvp-roadmap.md)。

---

## 9. 失败的处理

```
单元测试失败 → block merge
集成测试失败 → block merge
E2E nightly 失败 → P1 ticket，next-day fix
性能回归 > 5% → block release
安全扫描 high/critical → block merge
覆盖率下降 → 警告 + reviewer 决定
chaos 演练失败 → 运维改进 + runbook 更新
```

---

## 10. 与其他文档的链接

- 不变量编号：→ [01-architecture.md](./01-architecture.md) §7
- 协议测试场景：→ [02-protocol.md](./02-protocol.md)
- 状态机/恢复测试：→ [03-distributed-correctness.md](./03-distributed-correctness.md) §12
- 安全测试映射：→ [04-security-and-tenancy.md](./04-security-and-tenancy.md)
- 多源测试：→ [06-platform-and-ecosystem.md](./06-platform-and-ecosystem.md) §8
- Phase 计划：→ [08-mvp-roadmap.md](./08-mvp-roadmap.md)
