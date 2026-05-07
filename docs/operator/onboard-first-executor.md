# 首个 Executor Onboarding（mTLS Bootstrap）

> 用途：解决 FEAS-03 报告的 chicken-and-egg 问题 —— 第一台 executor 怎么拿到 enrollment_secret + mTLS 证书？
> 适用：v2.0 GA Phase 2 起；仅初次部署或加入新主机时使用。
> 估时：5 min（已有 controller running）。

---

## 0. 前置

- Controller 已部署并运行（见 `helm install` 步骤）
- `kubectl` 能访问 controller pod
- 已为该 executor host 准备好部署目标（K8s pod / VM / docker host）

---

## 1. 在 controller 端生成一次性 enrollment token

```bash
# 通过 kubectl exec 进入 controller pod
kubectl -n dlw-prod exec -it deploy/dlw-controller -- \
  dlw admin executor-token --create \
    --executor-id "host-12.local-worker-1" \
    --tenant-slug "default" \
    --expires-in 1h \
    --one-time
```

输出（示例）：

```
Enrollment token created:
  executor_id:   host-12.local-worker-1
  token:         <PASTE_TOKEN_FROM_STEP_1_HERE>  ← 256-bit hex, ONLY SHOWN ONCE
  expires_at:    2026-05-07T08:30:00Z
  one_time:      true (token destroyed after first use)

⚠️ Store this token securely. It cannot be retrieved later.
   Pass it to the executor host via secure channel (SSH paste / sealed Vault entry / etc.).
```

**安全要点**：

- `--one-time` 强制 token 仅可使用 1 次，注册后即销毁；防止泄漏后被恶意 register
- `--expires-in 1h` 让 token 短 TTL；如果运维拿到后超过 1h 没用就重新生成
- Controller 端审计：`SELECT * FROM enrollment_tokens WHERE executor_id_proposal=...`

---

## 2. 在 executor host 配置

### 2.1 K8s 部署（推荐）

```bash
# 把 token 注入 K8s Secret
kubectl -n dlw-prod create secret generic dlw-executor-enrollment \
  --from-literal=enrollment_token="<PASTE_TOKEN_FROM_STEP_1_HERE>" \
  --dry-run=client -o yaml | kubectl apply -f -

# Helm install executor
helm install dlw-executor charts/dlw \
  --namespace dlw-prod \
  --set controller.enabled=false \
  --set executor.enabled=true \
  --set executor.id="host-12.local-worker-1" \
  --set executor.enrollment.secret_name="dlw-executor-enrollment"
```

executor pod 启动时自动调 `/api/executors/register`，提交 CSR + enrollment_token，拿回客户端 mTLS 证书 + JWT。

### 2.2 VM / 裸机部署

```bash
# SSH 到 executor host
ssh dlw@host-12.local

# 把 token 写入 env 文件（chmod 600）
sudo tee /etc/dlw/enrollment.env <<EOF
DLW_ENROLLMENT_TOKEN=<PASTE_TOKEN_FROM_STEP_1_HERE>
DLW_EXECUTOR_ID=host-12.local-worker-1
DLW_CONTROLLER_ENDPOINT=https://controller.dlw.example.com
EOF
sudo chmod 600 /etc/dlw/enrollment.env

# 启动 executor systemd service
sudo systemctl start dlw-executor
sudo systemctl enable dlw-executor

# 验证：第一次启动时自动 register
sudo journalctl -u dlw-executor -f
# 期待看到："enrollment successful, mTLS cert issued, registered as host-12.local-worker-1"
```

---

## 3. 验证 enrollment 成功

```bash
# 在 controller 端
kubectl -n dlw-prod exec -it deploy/dlw-controller -- \
  dlw admin executor-list --filter status=joining

# 等待几秒后该 executor 应进入 healthy
kubectl -n dlw-prod exec -it deploy/dlw-controller -- \
  dlw admin executor-list --filter id=host-12.local-worker-1

# 验证 mTLS 证书已签发
kubectl -n dlw-prod exec -it deploy/dlw-controller -- \
  psql -tAc "SELECT executor_id, cert_fingerprint, epoch FROM executors WHERE id='host-12.local-worker-1'"

# 验证 enrollment_token 已销毁
kubectl -n dlw-prod exec -it deploy/dlw-controller -- \
  psql -tAc "SELECT * FROM enrollment_tokens WHERE token_hash=sha256('ent_...')"
# 应返回空行（one-time 已使用）
```

---

## 4. 故障排查

### 4.1 enrollment 失败：401 Invalid token

- token 已过期 → 重新生成
- token 已被使用 → one-time，必须重新生成
- 时钟漂移（CSR 时间戳超 60s）→ 同步 NTP

### 4.2 mTLS 证书签发失败

```bash
# 查 controller 日志
kubectl logs -l app.kubernetes.io/component=controller --tail=100 | grep enrollment

# 常见原因：
# - CA 私钥未加载（CSI Vault 配置错）
# - cert_ttl_hours 配置不合理（< 1h 会立即过期）
# - executor_id 与已存在记录冲突
```

### 4.3 SSL inspection 环境

如果 corp proxy 做 SSL inspection（详见 14 §1.5），mTLS fingerprint 校验会失败。需先配 TLS-bypass 列表，否则 executor 会 fail-fast（不变量 44）。

---

## 5. 批量 onboarding（>10 台 executor）

```bash
# 生成 N 个 token 写到 CSV
for i in $(seq 1 50); do
  HOST="host-${i}.local-worker-1"
  TOKEN=$(kubectl exec deploy/dlw-controller -- dlw admin executor-token --create \
    --executor-id "$HOST" --tenant-slug default --expires-in 24h --one-time --output json \
    | jq -r '.token')
  echo "$HOST,$TOKEN" >> /tmp/enrollment-tokens.csv
done

# 通过 ansible / saltstack 分发到各 host
ansible-playbook -i inventory/dlw-hosts deploy-executors.yml -e "tokens_csv=/tmp/enrollment-tokens.csv"
```

---

## 6. 凭证轮换

执行器 mTLS 证书 24h TTL 自动续签。如需强制轮换（被攻破嫌疑）：

```bash
# 撤销当前 cert（fingerprint 加入 CRL）
kubectl exec deploy/dlw-controller -- dlw admin executor-revoke-cert host-12.local-worker-1

# 在 executor host 重新跑 enrollment（同 §2 流程）
```

---

## 7. 与其他文档的链接

- mTLS 协议详细：[`04-security-and-tenancy.md`](../v2.0/04-security-and-tenancy.md) §2.2
- enrollment_token 数据模型：[`01-architecture.md`](../v2.0/01-architecture.md) §4.7.2
- 反向 WSS 通道：[`14-enterprise-network-and-rate-limit.md`](../v2.0/14-enterprise-network-and-rate-limit.md) §1
- SSL inspection 应对：[`14-enterprise-network-and-rate-limit.md`](../v2.0/14-enterprise-network-and-rate-limit.md) §1.5
