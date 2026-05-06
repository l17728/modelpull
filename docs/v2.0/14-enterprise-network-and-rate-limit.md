# 14 — 企业内网部署 / 限速探测 / 凭证池 / 别名 / Live Console

> 角色：企业典型拓扑（Executor 在公司内网，Controller 在外网或 DMZ）的端到端设计。
> 范围：反向隧道 / 网关限速维度探测 / 多凭证池 / 别名 / 实时控制台 / S3 源协作。
> 引入版本：**v2.1**（v2.0 仅提供基础 register + 心跳 polling，本文是工程化升级）。

---

## 0. 立项背景：典型企业拓扑

```
                  ┌──────────────────────────┐
                  │  Public Internet         │
                  │   (外网)                  │
                  │                          │
                  │   ● Controller (DMZ/cloud)│
                  │   ● HuggingFace            │
                  │   ● ModelScope             │
                  │   ● HF Mirror               │
                  └────────────┬─────────────┘
                               │ (HTTPS)
                  ┌────────────┴─────────────┐
                  │  Corp Gateway / Proxy     │
                  │  - 限速：?per-conn        │
                  │           ?per-IP         │
                  │           ?per-user       │
                  │  - 鉴权：proxy auth      │
                  │           （可选 NTLM/    │
                  │            Basic）        │
                  └────────────┬─────────────┘
                               │
                  ┌────────────┴────────────────────────────┐
                  │  Corp Internal Network (内网)            │
                  │                                          │
                  │  ●  Executor 1 ─ alias "GPU室 A worker 1"│
                  │  ●  Executor 2                          │
                  │  ●  Executor 3                          │
                  │  ●  ...                                  │
                  │                                          │
                  │  ●  内网 S3 mirror（可选源）             │
                  │  ●  内网 NFS / 训练集群（目的存储）     │
                  └─────────────────────────────────────────┘
```

5 个工程问题：

1. **反向连接**：Executor 内网，无入站 IP，控制器如何下发命令？（不是单纯的 10s 心跳轮询，需要近实时命令通道）
2. **限速维度未知**：Corp Gateway 限速可能按 connection / IP / user，影响子分片策略选择
3. **多凭证轮换**：如果限速维度是 user，需要本地配置多个 gateway 账号/HF token，让下载器轮换使用
4. **运维可见**：控制台需要详细实时日志，方便监控整体下载状况
5. **别名**：纯 IP / 主机名难记，运维需要 "GPU室 A worker 1" 这种业务别名
6. **S3 源切片**：内网 S3 mirror 直接拿 byte range 也要走 13 章的多 executor 协作

---

## 1. 反向控制通道

### 1.1 现状（v2.0）的不足

v2.0 设计：

- Executor → Controller：HTTPS POST 心跳每 10s，包含进度上报 + assignment 拉取（02 §4）
- Controller → Executor：**只能在心跳响应中下发命令**

问题：

- 用户取消任务 → controller 想 cancel → 必须等下一个心跳（最坏 10s 延迟）
- PlanOptimizer 决策（详见 13）想立即重新分配 → 同样最坏 10s
- ETA 偏离触发 hard trigger → 反应延迟主要是这个心跳间隔

### 1.2 v2.1 反向通道：长连 WSS

Executor 注册成功后，**主动开**一条 WebSocket 到 controller：

```
Executor                                          Controller
  │                                                  │
  │ ──── POST /api/executors/register ───────────►│
  │ ◄─── 200 + executor_jwt + ca_chain ─────────── │
  │                                                  │
  │ ──── WSS /ws/v1/executor                       │
  │       Sec-WebSocket-Protocol: bearer.<jwt>      │
  │       (出站连接，corp proxy 可穿透) ─────────►│
  │ ◄─── 101 Switching Protocols ─────────────────│
  │                                                  │
  │  〈持久双向通道〉                                │
  │                                                  │
  │ ◄── push: {type: "assign", subtask, ...}      │
  │ ◄── push: {type: "cancel", subtask}             │
  │ ◄── push: {type: "rebalance_chunk", ...}        │
  │ ◄── push: {type: "renew_sts_credentials", ...}  │
  │                                                  │
  │ ──► event: progress {bytes, speed_window}       │
  │ ──► event: subtask_complete {sha, etag, ...}    │
  │ ──► event: rate_limit_probe_result {...}        │
  │                                                  │
  │ ──► ping (每 20s; corp proxy 防 idle 关闭)      │
  │ ◄── pong                                          │
```

**关键性质**：

- 出站连接：满足"corp 内网无入站"约束（不变量 4 不破）
- WSS over HTTPS：穿透绝大多数 corp proxy（CONNECT method 或 透明代理都 OK）
- 双向：controller 可主动推命令；executor 实时上报事件
- 老的 HTTPS 心跳保留为 fallback：WSS 断线时退化到 10s polling

🔒 **不变量 35 (v2.1, DIST-V21-03)：每条 controller→executor push 必须携带 `target_executor_epoch`**

```jsonc
// Server push schema（必含字段）
{
  "type": "assign|cancel|abort|rebalance|renew_sts|drain",
  "target_executor_epoch": 14,         // ← 必需
  "optimization_generation": 42,        // ← 见 DIST-V21-04 fence
  "subtask_id": "...",
  "assignment_token": "...",
  // ... 其他字段
}
```

Executor 收到 push 时必须校验：

```python
async def on_push(msg):
    if msg["target_executor_epoch"] != self.epoch:
        # 旧 epoch 的命令（来自 reclaim 之前的 ws_session）
        await self.ws.close(code=1008, reason="epoch_mismatch")
        await self.reconnect_with_new_register()
        return
    # 正常处理
```

**WSS session 失效语义**：

- Controller 端：每次 `executors.epoch` 自增（re-register） → 强制 invalidate 该 executor 所有现存 ws_session（写 `executor_ws_sessions.closed_at`）
- 旧 ws_session 上的 push 一律不发；TCP 层若有 OS buffer 残留，executor 客户端通过 `target_executor_epoch` 校验拒收
- 数据模型：`executor_ws_sessions(epoch_at_connect)` 详见 01 §4.7.2

### 1.3 心跳通道协议简化

WSS 通道建立后：

- 进度上报频率从 10s/次 升到 1s/次（事件驱动；只有变化才推）
- 心跳合并优化（02 §4.4）改为基于事件 batch
- HTTPS 心跳改为每 60s 兜底（防 WSS 异常断线但 executor 没察觉）

### 1.4 Corp proxy 适配

Executor 配置：

```yaml
# /etc/dlw/executor.yaml
network:
  controller_endpoint: https://controller.dlw.example.com
  ws_endpoint: wss://controller.dlw.example.com/ws/v1/executor

  proxy:
    enabled: true
    url: http://corp-proxy.internal:8080
    # 如果 proxy 需要 auth，从下面 §3 凭证池里选
    auth_alias: corp_user_default

  # WSS 保持
  ws_ping_interval_seconds: 20            # corp proxy 闲置超时通常 60-300s
  ws_idle_close_handling: reconnect_immediate
  reconnect_backoff_min_s: 1
  reconnect_backoff_max_s: 30
```

支持以下穿透模式：

| 模式 | 配置 | 适用 |
|------|------|------|
| Transparent proxy | 无需特殊设置 | 网关透明代理 |
| Explicit HTTP CONNECT + Basic auth | `proxy.url` + auth_alias (basic) | 公司常见 squid/forward proxy |
| **NTLMv2** (v2.1, ENT-QA-01) | `auth_type: ntlm` | Windows AD 域内（金融/电信常见） |
| **Kerberos / SPNEGO** (v2.1, ENT-QA-01) | `auth_type: kerberos` + keytab | 企业级 SSO（金融、运营商） |
| SOCKS5 | `proxy.url=socks5://...` | 部分企业 |
| 跳板机 SSH 隧道 | 不在 v2.1 范围 | 极特殊场景 |

#### 1.4.1 NTLMv2 认证（ENT-QA-01）

```yaml
# /etc/dlw/credentials.yaml
gateway_accounts:
  - alias: corp_user_ntlm
    auth_type: ntlm
    domain: CORP                # NT 域名
    username: jdoe
    password: ${CORP_PASS}
```

实现：

- 用 `requests-ntlm2` 或 `httpx-ntlm` 库（前者社区维护，更稳）
- NTLM challenge-response 在 connection 级（非 request 级），WSS 长连场景适用
- **Phase 2.5 SPIKE**：在 squid + NTLM proxy 容器栈中跑通 WSS+CONNECT；测试集 I-ENT-AUTH-001..006

#### 1.4.2 Kerberos / SPNEGO（ENT-QA-01）

```yaml
gateway_accounts:
  - alias: corp_kerberos
    auth_type: kerberos
    keytab: /etc/dlw/corp.keytab
    principal: dlw@CORP.EXAMPLE.COM
    krb5_conf: /etc/krb5.conf
```

实现：

- 用 `requests-kerberos` + `gssapi`（需 system-level libkrb5 + libgssapi-krb5）
- container 部署需 mount `/etc/krb5.conf` + keytab + DNS 正向 PTR 配 KDC
- 自动 ticket 续期（默认 8h）
- **Phase 2.5 SPIKE**：MIT KDC 容器栈 + AD time skew 模拟

#### 1.4.3 测试要求（ENT-QA-01）

新增 `I-ENT-AUTH-001..006`（详见 07 §11.7）：

| ID | 场景 |
|----|------|
| I-ENT-AUTH-001 | NTLMv2 challenge-response 完整流程 |
| I-ENT-AUTH-002 | Kerberos ticket 自动续期 |
| I-ENT-AUTH-003 | AD time skew > 5min 触发 KRB5KDC_ERR_PREAUTH_FAILED |
| I-ENT-AUTH-004 | proxy 401 重协商（NTLM 三步握手中间断） |
| I-ENT-AUTH-005 | 多 alias 中混合 auth_type（basic + ntlm + kerberos）轮换 |
| I-ENT-AUTH-006 | keytab 文件权限错误 → executor 启动 fail-fast |

### 1.5 在 SSL Inspection 环境下的部署（v2.1, ENT-QA-02）

> Zscaler / Forcepoint / Symantec Web Gateway 等企业 SSL inspection 默认拦截解密所有出站 TLS。这与 04 §2.2 的 mTLS 客户端证书校验**根本互斥** —— proxy 解密后用自己的 CA 重新加密，executor 看到的是 proxy 的 cert，mTLS 失败。

#### 1.5.1 强制 TLS-bypass 列表

部署到此类企业前，必须协调 IT 把以下域名加入 SSL inspection 白名单（**不解密**）：

```
*.dlw.example.com               # controller HTTPS
*.dlw-ws.example.com            # WSS reverse channel
*.huggingface.co                # HF API
*.hf.co
*.modelscope.cn                 # ModelScope
*.hf-mirror.com
```

Helm value：

```yaml
network:
  tls_bypass_required_hosts:
    - api.dlw.example.com
    - ws.dlw.example.com
    - huggingface.co
    - cdn-lfs.huggingface.co
    - www.modelscope.cn
    - hf-mirror.com
```

#### 1.5.2 检测 mTLS 失效并 fail-fast

executor 启动时必须显式校验：

```python
def verify_mtls_path():
    """
    握手成功后取 server cert，对比预期 SHA-256 fingerprint。
    若 fingerprint 不匹配（说明被 proxy 解密重签）→ fail-fast，不静默重试。
    """
    cert_chain = ssl_socket.getpeercert(binary_form=True)
    actual_fingerprint = hashlib.sha256(cert_chain).hexdigest()
    expected = config.controller_cert_fingerprint
    if actual_fingerprint != expected:
        log.fatal(
            "TLS-bypass not configured for controller. "
            "SSL inspection is decrypting our mTLS — please ask IT to whitelist "
            f"{config.controller_endpoint} (see docs §1.5)."
        )
        sys.exit(1)
```

#### 1.5.3 测试

新增 **E2E-ENT-SSL-001**：mitmproxy 中转 + 校验 executor 启动时清晰报错（不静默 retry）。

#### 1.5.4 退化方案：纯应用层加密

若 IT 拒绝把域名加入 bypass 列表，可启用**应用层加密**：

- mTLS 退化为单向 TLS（接受 proxy 解密）
- 心跳 body 增加应用层加密（envelope encryption + per-message AEAD）
- 所有 secret 在 wire 上**永远** envelope-encrypted（即便 proxy 看到也是密文）
- 性能代价：每条心跳 +2-5ms 加解密 overhead

🔒 **不变量 44 (v2.1, ENT-QA-02)**：mTLS fingerprint 不匹配时 fail-fast；不允许静默接受 proxy 重签的证书。

### 1.6 安全

WSS 通道复用 mTLS + Executor JWT（04 §2.2）。**ws connect 时 WS 子协议握手携带 JWT**：

```
Sec-WebSocket-Protocol: bearer.eyJhbGciOiJFZERTQSI...
```

服务端校验逻辑同 04 §4.5。

🔒 **不变量 28：反向 WSS 通道与 HTTPS 心跳通道使用同一组 mTLS + JWT 凭证**
（避免双套凭证维护；JWT 续签作用于两个通道）

---

## 2. 网关限速维度探测

### 2.1 三种限速维度

| 维度 | 限速依据 | 对策 |
|------|--------|------|
| **per-connection** | 单 TCP 连接的速率上限 | 同一 executor 开**多个并发连接** |
| **per-IP** | 同一 source IP 的总速率上限 | **多个 executor 机器**（不同 IP 子网更佳） |
| **per-user** | 同一 gateway / source 鉴权用户的总速率 | **多个用户账号**轮换（详见 §3 凭证池） |

实际限速可能是组合：例如 "10 Mbps/连接 AND 100 Mbps/IP AND 1 Gbps/user"。

### 2.2 探测算法（v2.1 修订 — OR-V21-07）

> 原版本 ratio 阈值（如 `> 2.5`）在真实速度抖动 ±20% 下误判率 ~7%。v2.1 改为**配对 t-test** + 多次采样 median + 显式 confidence 公式。

**统计学基础**：

- 速度测量含噪声：`speed = μ ± σ`，`σ/μ ≈ 5-25%`（依赖网络）
- 多次独立测量取 median 比单次 ratio 鲁棒
- **paired t-test** 比"是否 > 阈值"更准确

启动期 + 周期重测。每个 (target_host, executor_ip_class) 组合维护一份探测结果。

```python
def probe_rate_limit_dimension(target_host: str, executor: Executor) -> RateLimitProfile:
    """
    用受控的小流量实验确定限速维度。耗时 ~30s，开销 ~50MB 流量。
    """
    # Step 0 (v2.1, ENT-QA-07): 真实 corp gateway 鲁棒性
    # - 突发宽容：前 8s 不限速（许多 token-bucket gateway 有 burst 配额）
    # - 测量长尾：丢弃前 8s 数据，再测稳态速度
    BURST_DISCARD_S = 8
    STEADY_STATE_DURATION_S = 30

    # Step 1: 单连接基线（仅取稳态）
    raw = measure(executor, target_host, n_connections=1, n_executors=1,
                  duration=STEADY_STATE_DURATION_S + BURST_DISCARD_S)
    speed_1conn = raw.steady_state_avg(discard_first_s=BURST_DISCARD_S)

    # Step 2: 同 executor 多连接（v2.1: 多次采样 + paired t-test）
    samples_1conn = [measure(executor, target_host, n_connections=1, n_executors=1)
                     for _ in range(N_SAMPLES)]   # N_SAMPLES = 5
    samples_4conn = [measure(executor, target_host, n_connections=4, n_executors=1)
                     for _ in range(N_SAMPLES)]
    # H0: speed_4conn = speed_1conn × n_conn（无连接限速时成立）
    # H1: speed_4conn / speed_1conn 显著小于 4
    from scipy.stats import ttest_rel
    ratios = [s4 / s1 for s4, s1 in zip(samples_4conn, samples_1conn)]
    median_ratio = median(ratios)
    t_stat, p_value = ttest_rel(samples_4conn, [s * 4 for s in samples_1conn])
    # 拒绝 H0（即 connection_limited=True）当 p_value < 0.05 且 median_ratio < 2.5
    connection_limited = (p_value < 0.05) and (median_ratio < 2.5)

    # Step 3: 同 IP 段、同 user，但开 2 个 executor 进程
    if has_sibling_executor_same_host(executor):
        speed_2procs = measure_with_siblings(target_host, n_processes=2)
        # 同 host 同 IP 不同进程：测试是否每进程独立 connection 池
        # 通常这步不增 IP / user 维度

    # Step 4: 不同 IP 的 executor
    if has_executor_other_ip(target_host):
        speed_2ip = measure_distributed(target_host, executor_count=2)
        ip_limited = (speed_2ip / speed_1conn) > 1.8

    # Step 5: 同 IP，不同 user 账号（如果 §3 凭证池有 ≥2 user）
    if executor.credential_pool.gateway_accounts >= 2:
        speed_2user = measure_with_user_rotation(target_host, executor, accounts=2)
        user_limited = (speed_2user / speed_1conn) > 1.8

    # Step 6 (v2.1, OR-V21-18): 双维度 cross-axis probe
    # 真实 gateway 常用 token bucket on multiple keys，叠加非线性
    if connection_limited and ip_limited:
        # n=2 IP × n=2 conn 是否真接近 4×？
        speed_2ip_2conn = measure_distributed(target_host, executor_count=2, n_connections=2)
        cross_axis_factor = speed_2ip_2conn / speed_1conn
        # 如果 < 3.5（理论 4）说明叠加非线性
        if cross_axis_factor < 3.5:
            warnings.append("non-linear multi-axis limit suspected")
            confidence *= 0.7  # 降低 combined 模式的可信度

    return RateLimitProfile(
        target_host=target_host,
        connection_limited=connection_limited,
        ip_limited=ip_limited,
        user_limited=user_limited,
        per_conn_speed_bps=speed_1conn,
        per_ip_speed_bps=speed_2ip if ip_limited else None,
        per_user_speed_bps=speed_2user if user_limited else None,
        measured_at=now(),
        confidence=compute_confidence(measurements),
    )
```

**Confidence 公式（v2.1 修订）**：

```python
def compute_confidence(samples_1conn, samples_4conn, p_value, concurrent_traffic_ratio):
    """
    confidence ∈ [0, 1]：探测结果可信程度。
    - 高样本数、低噪声、低并发干扰 → 高 confidence
    - 探测期间生产流量混入越多 → 低 confidence
    """
    # CV (coefficient of variation) of measurements
    cv_1 = stdev(samples_1conn) / mean(samples_1conn)
    cv_4 = stdev(samples_4conn) / mean(samples_4conn)
    cv_avg = (cv_1 + cv_4) / 2

    # 样本充足度
    sample_factor = min(1.0, N_SAMPLES / 5)

    # 显著性
    significance_factor = 1 - p_value if p_value < 0.05 else 0.5

    # 并发干扰扣分
    interference_penalty = max(0, concurrent_traffic_ratio - 0.1)

    confidence = sample_factor * (1 - cv_avg) * significance_factor * (1 - interference_penalty)
    return clamp(0, 1, confidence)
```

`concurrent_traffic_ratio` = 探测期间该 target_host 的真实任务流量 / probe 流量。详见 §2.5 限速画像驱动调度（confidence < 0.6 时退化为保守策略）。

### 2.3 探测频率与集群级协调（v2.1 修订 — DIST-V21-05）

> 原版本"每 controller 每天 5GB" 在多 controller 集群下总预算无控（10 controller × 5GB = 50GB/天）；同时多 controller 同时探测 + 互相挤占造成 measurement contamination。

**集群级协调**：

```
集群中的 controller 用 PG advisory lock 选 probe leader：
  SELECT pg_try_advisory_lock(hash('probe_leader'))

当前 leader 负责所有 (target_host, ip_class) 探测；
其他 controller 仅消费探测结果（从 rate_limit_probes 表读最新行）。
```

🔒 **不变量 43 (v2.1, DIST-V21-05)**：同时只有 1 个 controller 是 probe leader；leader 异常时通过 advisory lock TTL 自动转移。

**预算与频率**：

- 启动后第一次：**强制探测**（leader 执行；每个 target_host）
- 周期重测：每 6h
- 触发重测：leader 收到任一 controller 上报"连续 5 分钟实测速度 < 探测预期 50%"
- **集群级预算**（不再 per-controller）：每天 ≤ 5GB 总探测流量
  ```sql
  -- 详见 01 §4.7.5 probe_budget 表
  SELECT bytes_used, bytes_limit FROM probe_budget WHERE day = CURRENT_DATE;
  ```
  超额时新探测请求拒绝；既有探测结果继续使用直到过期
- **预算耗尽期间新 target 处置**（v2.1, ENT-QA-18）：
  - 优先用最近同 `executor_ip_class` 邻居的 RateLimitProfile（地理 / 网络环境近似）
  - confidence × 0.5（标记为推断而非测量）
  - 写入 `rate_limit_probes` 时 `measurement_started_at = NULL`，标志推断结果

### 2.4 数据模型

```sql
CREATE TABLE rate_limit_probes (
    id                    BIGSERIAL PRIMARY KEY,
    target_host           VARCHAR(256) NOT NULL,    -- 'huggingface.co' / 'cdn-lfs...' / 'controller.dlw...'
    executor_ip_class     VARCHAR(64),               -- 'corp-net-A' / 'corp-net-B' (subnet 分类)
    user_account_alias    VARCHAR(64),               -- 用了哪个 §3 别名
    connection_limited    BOOLEAN,
    ip_limited            BOOLEAN,
    user_limited          BOOLEAN,
    per_conn_speed_bps    BIGINT,
    per_ip_speed_bps      BIGINT,
    per_user_speed_bps    BIGINT,
    confidence            FLOAT,                     -- 0.0-1.0
    measured_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at            TIMESTAMPTZ NOT NULL       -- 探测结果 6h 失效
);

CREATE INDEX idx_rl_probe_lookup ON rate_limit_probes(target_host, executor_ip_class, expires_at);
```

### 2.5 限速画像驱动调度（与 13 章联动）

PlanOptimizer（13 §4.1）拿到限速画像后，**子分片策略**变成：

```python
def optimal_split_strategy(file, profile, available_executors):
    if profile.connection_limited and not profile.ip_limited and not profile.user_limited:
        # 只限连接：单 executor 多连接最便宜
        return SplitStrategy(mode="multi_conn_single_executor", k_conn=8)

    if profile.ip_limited and not profile.user_limited:
        # 限 IP：必须多 executor 机器
        return SplitStrategy(mode="multi_executor", executor_picks=top_distinct_ip(available_executors))

    if profile.user_limited:
        # 限 user：每 chunk 用不同 gateway account
        return SplitStrategy(mode="multi_user_account",
                             account_picks=executor.credential_pool.gateway_accounts[:k])

    if profile.connection_limited and profile.ip_limited:
        # 双维度：先选不同 IP 的 executor，再每个上多连接
        return SplitStrategy(mode="combined",
                             executor_picks=top_distinct_ip(available_executors),
                             k_conn_per_executor=8)

    # 都不限 → 任意切
    return SplitStrategy(mode="speed_optimal", ...)
```

**关键**：13 §4.1 的 `compute_split_plan` 现在多一个**输入参数 `RateLimitProfile`**，决定切多少份和如何分配。

🔒 **不变量 29：子分片策略必须考虑限速画像；不可在限速维度未知时盲目并发**
（避免 "1 个 user 限速 100Mbps，开 16 conn 全部抢同一 100Mbps 池" 的徒劳）

---

## 3. 本地凭证池

### 3.1 设计目标

让 executor 持有**多组凭证**（gateway / source / HF），按需选择，配合 §2 限速画像。

🔒 **不变量 30：执行器本地凭证不出本机**
（凭证池只存在 executor 本地配置；controller 仅知"alias"，不持具体值。这与 04 §3.1 HF Token reverse-proxy 的 controller-side token 是**两套不同凭证**：reverse-proxy token 是 tenant-级别用于 HF 鉴权；本地凭证池是 executor 用于穿透 corp gateway）

### 3.2 配置文件结构

```yaml
# /etc/dlw/credentials.yaml  （或 /etc/dlw/credentials.d/*.yaml）
# 仅本机可读，权限 600，executor 进程拥有

# Corp proxy / gateway 鉴权账号池
gateway_accounts:
  - alias: corp_user_a
    auth_type: basic
    username: jdoe
    password: ${CORP_PASS_A}        # 从 env 读，避免硬编码
    bandwidth_quota_mbps: 1000      # admin 知道每账号配额
  - alias: corp_user_b
    auth_type: basic
    username: jsmith
    password: ${CORP_PASS_B}
    bandwidth_quota_mbps: 1000
  - alias: corp_kerberos
    auth_type: kerberos
    keytab: /etc/dlw/corp.keytab
    principal: dlw@CORP.EXAMPLE.COM

# HuggingFace token 池（用于绕过 HF per-user 限流；普通场景下 controller-managed token 已够）
hf_tokens:
  - alias: hf_user_team_a
    token: ${HF_TOKEN_A}
    rate_limit_mbps: 500            # HF 也有 per-user 限速
  - alias: hf_user_team_b
    token: ${HF_TOKEN_B}

# 内网 S3 mirror 凭证池
s3_credentials:
  - alias: corp_s3_aksk_a
    source_id: corp_mirror
    access_key_id: ${S3_AK_A}
    secret_access_key: ${S3_SK_A}
  - alias: corp_s3_aksk_b
    source_id: corp_mirror
    access_key_id: ${S3_AK_B}
    secret_access_key: ${S3_SK_B}

# 默认选择策略
selection_policy:
  default_gateway: corp_user_a
  rotation: round_robin            # round_robin / least_used / explicit
  switch_on_429: true              # 触发 429 时自动切换
```

### 3.3 上报给 controller

executor 启动时把**仅 alias + meta** 上报给 controller（不上报 secret）：

```http
POST /api/executors/register
{
  "executor_id": "host-12.local-worker-1",
  "credential_pool_summary": {
    "gateway_aliases": ["corp_user_a", "corp_user_b", "corp_kerberos"],
    "hf_token_aliases": ["hf_user_team_a", "hf_user_team_b"],
    "s3_credential_aliases": [
      {"alias": "corp_s3_aksk_a", "source_id": "corp_mirror"},
      {"alias": "corp_s3_aksk_b", "source_id": "corp_mirror"}
    ]
  }
}
```

Controller 把 aliases 存到 `executors.credential_pool` JSONB 字段。

### 3.4 调度时下发"用哪个 alias"

Controller 派任务时只指定 alias：

```json
{
  "subtask_id": "uuid",
  "use_credentials": {
    "gateway_alias": "corp_user_b",
    "hf_token_alias": "hf_user_team_a",
    "s3_credential_alias": "corp_s3_aksk_a"
  },
  ...
}
```

executor 收到后查本地凭证池表 → 取 secret → 发 HTTPS。

### 3.5 选择策略

| 场景 | 策略 |
|------|------|
| 普通下载（限速画像未做） | `default_gateway` |
| §2 探测显示 user-limited | optimizer 派任务时 round-robin gateway_aliases |
| 同一文件子分片到 K 个 chunk | controller 给每个 chunk 不同 alias（保证不冲突） |
| 触发 429 / 401 | executor 自动切下一个 alias 重试一次，并上报"alias X 触发 429" |

### 3.6 凭证轮换 / 删除（v2.1 修订 — DIST-V21-07）

> 原版本 "alias 删除后完成本 chunk 才停" 在 alias 已被外部 revoke（即时 401）时与"401 自动 fallback to next alias"语义冲突 → 抖动循环。修订为 **drain → purge 两阶段**协议。

凭证轮换流程（无停机）：

```
1. admin 在 executor 主机上 admin alias 'corp_user_a' 把 password 改成新值
2. 重新加载 credentials.yaml （SIGHUP 信号）— 仅更新 secret，alias 保留
3. executor 通过 WSS 上报 "alias corp_user_a secret_updated"
4. controller 发起一次轻量探测确认新 secret 工作
5. 完成
```

**Alias 删除两阶段（drain → purge）**：

```
Phase 1: drain
  ├─ admin 修改 executor.yaml 移除 alias 'corp_user_a'
  ├─ SIGHUP 触发：executor 不立即从内存删除，而是标记 alias='draining'
  ├─ executor 通过 WSS 发 {type: 'creds_change_pending',
  │                         removed_aliases: ['corp_user_a']}
  ├─ controller ACK 后停止派带该 alias 的新任务
  └─ executor 等待所有 in-flight 引用该 alias 的 chunk 完成

Phase 2: purge
  ├─ in-flight 全部完成后，executor 真正从内存清除 secret
  └─ 通过 WSS 发 {type: 'creds_change_committed',
                  removed_aliases: ['corp_user_a']}
```

🔒 **不变量 31（修订 v2.1, DIST-V21-07）**：alias 移除走 drain→purge 两阶段：

- **drain 阶段**：controller 不再派任务但 in-flight 继续；401 期间不算 alias-failure（避免与下一阶段重复触发）
- **purge 阶段**：内存中真正清除 secret；后续误派此 alias 的请求立即 fail，触发 reschedule（而非 retry-with-next-alias，因为 alias 已不存在）
- **drain 期间 alias 被外部 revoke**（admin gateway 端禁用账号 → 401）：executor 检测 401 → 立即 abort 该 chunk → upgrade 到 purge 阶段（不等 in-flight 完成）

### 3.7 集中化凭证管理（v2.1, ENT-QA-05）

> 200+ executor 主机 SSH + 文件改 + SIGHUP 不可扩展。生产部署强烈推荐 **集中化凭证分发**。

支持三种 source：

| source | 描述 | 适用 |
|--------|------|------|
| `file` | `/etc/dlw/credentials.yaml` 静态文件（v2.0 默认） | dev / 小集群 |
| `vault` | Vault Agent sidecar 注入；executor 仅读 cached file | 中型集群（Vault 已有） |
| `external_secrets` | K8s ExternalSecrets Operator 同步到 Secret → CSI mount | K8s 大集群 |

Helm value：

```yaml
credentials:
  source: vault            # file | vault | external_secrets
  vault:
    addr: https://vault.corp.example.com
    role: dlw-executor
    secret_path: secret/data/dlw/credentials
    refresh_interval_seconds: 300
    fallback_to_file: true     # vault 不可达时回退本地 cached file
```

**自动轮换**：

- Vault 模式：Vault Agent watch secret 变化 → 写本地 cached file → executor 自动 SIGHUP
- ExternalSecrets：CSI volume 自动更新 → executor inotify 触发 SIGHUP
- 不再需要 admin 手动 SSH 改文件

🔒 **不变量 30（修订 v2.1, ENT-QA-05）**：本地凭证池可由集中 secret store 派生，但**已派发到 executor 内存的 secret 不上报 controller**（不变量 30 核心约束不变）。

### 3.8 安全审计

每次凭证使用写：

```sql
CREATE TABLE credential_usage_log (
    id              BIGSERIAL PRIMARY KEY,
    executor_id     VARCHAR(64) NOT NULL,
    alias           VARCHAR(64) NOT NULL,
    alias_type      VARCHAR(16) NOT NULL,   -- gateway / hf_token / s3
    subtask_id      UUID,
    bytes_through   BIGINT NOT NULL DEFAULT 0,
    auth_failures   INT NOT NULL DEFAULT 0,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at        TIMESTAMPTZ
);
```

audit_log 同时记录 (action=credential.used, alias=...) — 便于事后追责凭证泄漏路径。

---

## 4. 别名系统

### 4.1 别名对象

| 对象 | 系统 ID | 别名场景 | 例 |
|------|--------|---------|-----|
| Executor | `host-12.local-worker-1` | 业务命名 | "GPU室 A worker 1" / "GPU-Pod-3" |
| Storage backend (目的) | `id=5` | 部门/集群命名 | "训练集群 A NFS" / "Inference Cluster S3" |
| Source (源) | `id=corp_mirror` | 友好命名 | "公司 HF 镜像" |
| Tenant | `slug=team-a` | 部门名 | "AI Lab" |
| User | OIDC sub | 中文姓名 | "张三 (zhangsan@corp)" |
| Project | id | 项目代号 | "Yi-9B 训练" |

### 4.2 数据模型

把 `display_name` / `alias_*` 字段加到现有表，不破坏 ID：

```sql
ALTER TABLE executors ADD COLUMN display_name VARCHAR(128);
ALTER TABLE executors ADD COLUMN location VARCHAR(128);   -- e.g., "GPU 机房 A 8 楼"
ALTER TABLE executors ADD COLUMN tags JSONB DEFAULT '[]'; -- ["a100", "production"]

ALTER TABLE storage_backends ADD COLUMN display_name VARCHAR(128);
ALTER TABLE storage_backends ADD COLUMN destination_label VARCHAR(256);
-- 例：destination_label = "192.168.1.50:/data/models" + display_name = "训练集群 A NFS"

-- source_drivers 是配置级（不在 DB），但 sources.yaml 加 display_name:
# sources:
#   - id: corp_mirror
#     display_name: "公司内部镜像（北京机房）"
#     ...
```

### 4.3 UI 显示规则

UI 优先显示 `display_name`，鼠标悬停显示原 ID。在选择器、列表、详情页全部生效。

例任务详情页节点参与列表：

```
节点参与
┌─────────────────────────────────────────────────────────────────┐
│ 💚 GPU室 A worker 1     下载中 3 文件   1.2 GB/s   (host-12.local)│
│ 💚 GPU室 B worker 2     上传中 1 文件             (host-21.local)│
│ ⚠️ GPU室 A worker 4     degraded                  (host-12.local)│
└─────────────────────────────────────────────────────────────────┘
```

### 4.4 编辑权限

| 别名类型 | 谁能编辑 |
|---------|---------|
| Executor | tenant_admin（默认）/ system_admin |
| Storage backend | tenant_admin |
| Source | system_admin（影响全租户） |
| Tenant | system_admin |
| User | 自己 / tenant_admin |
| Project | project_owner / tenant_admin |

每次编辑写 audit_log：`action=alias.update`。

### 4.5 创建任务时使用别名

任务创建表单的所有选择器都展示别名：

```
Storage Backend 目的地：
  [ "训练集群 A NFS" (192.168.1.50:/data/models) ▾ ]
  [ "Inference Cluster S3" (s3://infer/models) ▾ ]
  [ "default-team-a-prod-s3" ▾ ]
```

API 仍按 ID（兼容）：

```http
POST /api/tasks
{
  "storage_id": 5,                 # 内部 ID
  ...
}
```

---

## 5. 实时控制台（Live Console）

### 5.1 目标

运维需要"集群此时此刻在干嘛"的视图。当前 v2.0 已经有 Loki 日志（05 §1.2）和 Prometheus，但运维想要**一站式实时滚动**界面。

### 5.2 UI 设计

新增页面 `/console`，仅 admin / operator 可访问：

```
┌────────────────────────────────────────────────────────────────────┐
│ 🖥 Live Console                                       连接: ⬤ 在线 │
├────────────────────────────────────────────────────────────────────┤
│ 过滤:                                                               │
│  组件: [全部 ▾] [controller / executor:host-12-w1 / scheduler / ...] │
│  级别: [INFO+ ▾]                                                    │
│  Task: [可选 task_id 过滤 ▾]                                        │
│  搜索: [_______________________________________]                   │
│  [▶ 暂停]  [▼ 自动滚动]  [⬇ 导出 24h]                             │
├────────────────────────────────────────────────────────────────────┤
│ 14:32:15.123 [controller]  INFO  task.create task=7e57a3f8 user=…   │
│ 14:32:15.234 [executor:GPU室A-w1] INFO heartbeat sent              │
│ 14:32:15.456 [scheduler]   INFO  CAS-then-enqueue subtask=uuid-…    │
│ 14:32:15.512 [optimizer]   INFO  bottleneck file=model-001 eta=90s  │
│ 14:32:15.678 [optimizer]   WARN  HF Mirror throttled, switch to MS  │
│ 14:32:16.001 [executor:GPU室A-w1] INFO chunk 5/8 done sha=abc...    │
│ 14:32:16.123 [hf_proxy]    DEBUG GET /repo/.../resolve/...          │
│ 14:32:17.456 [executor:GPU室B-w2] ERROR upload_part 500 retry...    │
│  ...                                                                │
│                                                          [更多 ▼]   │
├────────────────────────────────────────────────────────────────────┤
│ 当前并发任务 5 · executor 健康 9/10 · 优化器决策 12/分              │
│ 关联：[Grafana 全景] [Loki 完整查询] [Trace search]                 │
└────────────────────────────────────────────────────────────────────┘
```

### 5.3 数据源协议

新 endpoint：

```http
GET /api/admin/console/stream?components=controller,executor:host-12-w1&level=INFO&task_id=...
Accept: text/event-stream

event: log
data: {
  "timestamp": "2026-05-06T14:32:15.123Z",
  "component": "controller",
  "level": "INFO",
  "event": "task.create",
  "task_id": "7e57a3f8-...",
  "user_id": 42,
  "trace_id": "c0ffee...",
  "message": "Task created: deepseek-ai/DeepSeek-V3"
}

event: log
data: {...}
```

实现：

- Controller 内嵌 in-memory ring buffer（最近 1 万条日志）
- WSS / SSE 把符合 filter 的日志推送到客户端
- 同时 sink 到 Loki（已有 v2.0 设计）作为长期存储
- 流量控制：单客户端最多 100 条/秒

### 5.4 性能保护

```yaml
console:
  ring_buffer_size: 10000
  per_client_rate_limit: 100        # logs/sec
  per_client_max_session_minutes: 60
  global_concurrent_clients: 10
  default_level_filter: INFO        # DEBUG 默认隐藏（量太大）
  warmup_from_loki:
    enabled: true                    # v2.1, ENT-QA-24
    fetch_minutes: 5                 # standby 提升后从 Loki 拉最近 5min 预热 ring buffer
```

**HA 切换时 Live Console 不丢历史**（ENT-QA-24）：standby 提升时 ring buffer 为空 → 从 Loki tail 最近 5min logs 预热 → 用户 reconnect 后能看到 HA 切换前的事件。

### 5.5 隐私 / 合规

- console 看到的日志已经经过 04 §9.3 redactor（无 token / AK / 密码）
- 仅 admin / operator 可访问
- console 操作本身写 audit_log（action=console.view）

---

## 6. S3 源切片（已有能力的明确表达）

> 注：S3 源（如 corp_mirror）作为**数据源**的多 executor 多连接切片下载，13 §5 的协议**完全适用**。本节明确说明 UI 输入。

### 6.0 Source 生命周期与作用域（v2.1, X-CONS-12）

> 区分两类 source，避免文档间概念漂移。

| 类型 | 定义位置 | 生命周期 | 作用域 |
|------|---------|---------|------|
| **全局 Source** | `sources.yaml` 配置文件（06 §1.12） | 持久；admin 修改后 reload | 全租户 / 全任务 |
| **Ad-hoc Source**（v2.1 新增） | DB `ad_hoc_sources` 表（01 §4.7.3） | 任务级；task 完成 / 删除时一并清理 | 仅创建该 task 的任务 |

Ad-hoc Source 的关键性质：

- 不写入 `sources.yaml`（避免与全局配置冲突）
- 仅在 task 创建时指定（详见 §6.1）
- task 终态后 7 天自动清理（保留窗口便于事后审计 / 回放）
- 限速画像（§2）共享：ad_hoc 与全局共用 `target_host` key 的 RateLimitProfile（避免重复探测）

### 6.1 创建任务时指定 S3 源路径

任务创建表单"源策略"区域增加：

```
源策略
  ○ 仅 HuggingFace
  ● 自动多源加速
  ○ 自定义
  ○ 仅自托管 mirror
  ○ S3 直连下载（高级）   ← 新增
       Bucket: [_____________________]
       Path:   [models/team-a/...]
       Region: [cn-north-1 ▾]
       Credential alias: [corp_s3_aksk_a ▾]   ← 来自 §3 池
       Range request: ✓ 支持
       多 executor 切片: [✓] (8 路)
```

填后任务创建时，controller 注册一个临时 source（仅本任务有效）：

```python
ad_hoc_source = SourceDriver.s3_direct(
    bucket=...,
    prefix=...,
    region=...,
    credentials_alias=...,    # 提示 controller 把这个 alias 派给执行器
    parallel_downloads=8,
)
```

后续走 13 章正常 multipart 协议。

### 6.2 与限速探测的联动

S3 直连源 + corp gateway 在路径上 → §2 限速探测对该 S3 host 也适用。如果探测出 IP-limited，optimizer 知道要在多 executor 上切。

### 6.3 与 HF SHA256 真值（不变量 11）的关系

S3 直连源**通常不持有官方 HF sha256**。两种处理：

a. 用户上传时已计算 sha → 存为 S3 object metadata `x-amz-meta-sha256` → 任务校验时读取
b. 用户允许任务执行时下载完计算 sha → 与 HF 上对应 repo+revision 比对（如果两者的 model 是同一份）
c. 用户显式 `--trust-non-hf-sha256`（不变量 13 例外）

UI 在创建表单显示该模型在 HF 上的 sha；若 S3 内容与之不一致 → 任务进 `failed`。

---

## 7. 端到端典型场景

### 7.1 场景：内网 100 节点下载 700GB 模型

```
1. Admin 在 controller 创建任务：
   - Repo: deepseek-ai/DeepSeek-V3
   - Revision: 自动从 HF 解析 sha
   - Storage: "训练集群 A NFS" (alias)
   - Source: 自动多源（HF + ModelScope + 内网 corp_mirror）

2. Controller 选 executor 子集（10 台 GPU 室 A worker）：
   - 每台已通过 WSS 注册（出站连接）
   - credential_pool 上报：每台 2 个 corp_user_alias、1 个 hf_token

3. Controller 调用 §2 探测：
   - 测试发现：corp gateway 是 user-limited（每 user 1Gbps）
   - 单连接 200Mbps，叠加 5 连接达到 1Gbps（user 限制）
   - 不同 user 可叠加（10 user × 1Gbps = 10Gbps 理论上限）

4. PlanOptimizer 决策（13 章）：
   - 文件级：163 个 file 用 LPT 分到 10 个 (executor, source)
   - 大文件 (>100MB) chunk-level 8 路切：每路用不同 (executor, gateway_alias)
   - 16GB 文件：8 个 sub-chunk × 不同 alias = 8Gbps 聚合速度

5. 下载执行：
   - 每个 sub-chunk 进入对应 executor，executor 用对应 alias 发起请求
   - GET Range from source → controller HF proxy or S3
   - 响应流式落 .parts/ → upload as part to destination NFS（这步通过 multipart-emulation 或单 executor 拼装，因 NFS 非 S3 协议）

6. 实时监控：
   - admin 打开 Live Console → 实时看 10 个 executor 各自速度
   - 任务详情页"决策日志"展示每次 optimizer 切换原因
   - PlanOptimizer 探测到 alias B 突遇 401（密码改了）→ 自动切到 alias C，并在 Live Console 标 WARN
```

### 7.2 场景：跨地域内网 + 镜像合规

```
1. 北京内网 + 上海内网两组 executor，都连同一个 cloud controller
2. 任务由北京用户创建，destination 是北京内网 NFS
3. PlanOptimizer 选 executor 时优先北京（latency 低）
4. 上海 executor 也参与下载（带宽贡献）但不直接写 destination
   → 通过：controller 协调，所有 executor 写到一个共享 S3 staging bucket
   → 完成后 controller 触发 S3 → 北京 NFS 的最终落盘（一次性大块传输）
5. 整个过程 corp 网关穿透通过 §1.4 explicit proxy 配置
```

---

## 8. 限制与已知问题

| ID | 限制 | 缓解 |
|----|------|------|
| ENT-01 | WSS 在某些 corp proxy 环境下被 break（如 deep packet inspection） | 退化到 long-poll heartbeat；增加 60s 兜底 polling |
| ENT-02 | 限速探测自身耗带宽 ~50MB/次 | 探测预算 ≤ 5GB/天/controller；6h 周期 |
| ENT-03 | 凭证轮换需 SIGHUP（不能动态注入） | v2.2 支持 hot-reload via WSS push |
| ENT-04 | Live Console 仅看 ring buffer（最近 10000 条） | 长期需 Loki；console 是"近实时" UI |
| ENT-05 | 别名表无 i18n 多语言 | display_name 自由文本；用户自己写中文/英文 |
| ENT-06 | 不同 corp gateway user 的"是否独立限速"必须实验性确认 | 探测算法的 confidence 字段标记不确定性 |
| ENT-07 | NFS 目的地不支持 multipart 协议 → 需要 single-executor 拼装或 S3 staging | 文档化两种降级方案 |
| ENT-08 | 凭证池本地存储，主机 compromise 即泄漏 | 强制 chmod 600 + 推荐 `cryptography.fernet` 加密文件（开发者承担解密 key 管理） |

---

## 9. 配置示例（完整 executor.yaml）

```yaml
# /etc/dlw/executor.yaml
network:
  controller_endpoint: https://controller.dlw.example.com
  ws_endpoint: wss://controller.dlw.example.com/ws/v1/executor
  proxy:
    enabled: true
    url: http://corp-proxy.internal:8080
    auth_alias: corp_user_default
  ws_ping_interval_seconds: 20

executor:
  id: host-12.local-worker-1
  display_name: "GPU室 A worker 1"
  location: "8 楼 GPU 机房 A"
  tags: ["a100-80g", "prod"]
  parts_dir: /data/dlw/parts
  max_concurrent_subtasks: 5

credentials_file: /etc/dlw/credentials.yaml

logging:
  level: INFO
  format: json
  push_to_console: true            # Live Console 数据源
  loki_endpoint: http://loki.observability:3100
```

```yaml
# /etc/dlw/credentials.yaml （chmod 600）
gateway_accounts:
  - {alias: corp_user_a, auth_type: basic, username: jdoe, password: ${CORP_PASS_A}}
  - {alias: corp_user_b, auth_type: basic, username: jsmith, password: ${CORP_PASS_B}}
  - {alias: corp_user_default, auth_type: basic, username: dlw, password: ${CORP_PASS_DEFAULT}}

hf_tokens:
  - {alias: hf_user_team_a, token: ${HF_TOKEN_A}}

s3_credentials:
  - alias: corp_s3_aksk_a
    source_id: corp_mirror
    access_key_id: ${S3_AK_A}
    secret_access_key: ${S3_SK_A}

selection_policy:
  default_gateway: corp_user_default
  rotation: round_robin
  switch_on_429: true
```

---

## 10. 测试要点（详见 [07-test-plan.md](./07-test-plan.md) §11）

- 反向通道：WSS 断线后退化到 polling；30 分钟内自动重连成功率 ≥ 99%
- 限速探测：在合成 gateway 环境下能正确识别三种维度，混合维度 confidence 标记
- 凭证池：alias 增删 + SIGHUP 重载不丢任务；切换到无效 alias 触发 fallback
- 别名：display_name 在 UI 全部位置正确显示；并发 alias rename 不冲突
- Live Console：100 logs/s × 10 客户端不影响 controller 主流程
- S3 直连源：8 路并发切片；某 part 401 时切换 credential alias 自动重试

---

## 11. Roadmap 定位

### v2.0 不做

- 仅基础 register + 10s heartbeat polling
- 凭证不分 alias，单 HF token，单 gateway proxy
- 别名仅 storage backend `name` 字段（无 display_name）
- Live Console 不存在（仅 Loki）

### v2.1 first-class

本文全部内容（反向 WSS / 限速探测 / 凭证池 / 别名 / Live Console）。

### v2.2 进阶

- WSS 凭证 hot-reload（无需 SIGHUP）
- 限速探测的 ML 预测（已知 `${day_of_week, hour_of_day}` → 预期限速）
- 多区域 controller 协调（详见 06 §9 跨地域）
- 别名 i18n 多语言

---

## 12. 与其他文档的链接

- 不变量：→ [01-architecture.md](./01-architecture.md) §7（新增 28-31）
- 协议（WSS 反向通道）：→ [02-protocol.md](./02-protocol.md) §5
- mTLS / JWT 安全：→ [04-security-and-tenancy.md](./04-security-and-tenancy.md) §2
- HF Token reverse-proxy 与本文凭证池的差异：→ [04-security-and-tenancy.md](./04-security-and-tenancy.md) §3.1
- 限速画像驱动 PlanOptimizer 子分片：→ [13-adaptive-download-optimization.md](./13-adaptive-download-optimization.md) §4.1
- Live Console 与 Loki 的分工：→ [05-operations.md](./05-operations.md) §1.2
- 别名在 UI 显示：→ [10-frontend-wireframes.md](./10-frontend-wireframes.md)
- 测试矩阵：→ [07-test-plan.md](./07-test-plan.md) §13
