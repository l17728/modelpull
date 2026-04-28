# 09 — v1.x → v2.0 数据迁移与上线策略

> 角色：DBA / SRE / 后端 Tech Lead 计划升级。
> 范围：DB schema 迁移、数据回填、应用代码切换、灰度策略、回滚预案。

---

## 0. 前提与基线

### 0.1 v1.x → v2.0 主要 schema 差异

| 类别 | v1.x | v2.0 | 影响 |
|------|------|------|------|
| 多租户 | 无 `tenant_id` | 全表加 `tenant_id` 外键 | 全部业务表 |
| 任务 owner | 无 | `owner_user_id`, `project_id` | `download_tasks` |
| 路径 | `target_dir` 字段 | `storage_id` + `path_template` | `download_tasks` |
| Fence token | 无 | `executor_epoch`, `assignment_token` | `file_subtasks`, `executors` |
| 状态机 | `transferring` 状态 | 统一为 `uploading`+`verifying_*` | `file_subtasks.status` |
| Multipart | 不持久化 | `multipart_upload_id` 列 | `file_subtasks` |
| 实际 sha | 无 | `actual_sha256` | `file_subtasks` |
| 多源 | 无 | `source_id`, `subtask_chunks` 表 | `file_subtasks` + 新表 |
| 用户身份 | 无 / 简单 token | OIDC + `users` 表 | 新表 |
| 配额 | 单任务级 | 租户级 + `usage_records` | 新表 |
| 审计 | 无 | `audit_log` 链式哈希 | 新表 |
| Storage | 简单 dict | `storage_backends` 表 + 加密 | 新表 |
| HF Token | 散落 env / 字段 | `tenant_secrets.hf_token_encrypted` | 新表 |

### 0.2 v1.x 数据规模假设

| 资产 | 典型规模 |
|------|---------|
| 已完成任务 | 1k-10k |
| 进行中任务 | 0-50 |
| 历史 subtask | 100k-1M |
| Executor | 10-100 |
| 已存储文件总量 | 100GB-100TB |

数据量决定迁移窗口：1M subtask 在 PG 上 alembic upgrade 大约 5-15 分钟（含索引重建）。

### 0.3 升级目标

| 目标 | 量化 |
|------|------|
| 数据零丢失 | 已完成任务 + 已存储文件 100% 保留 |
| 进行中任务尽量不打断 | 升级后能 resume 至少 80% 进行中任务 |
| 服务停机窗口 | ≤ 30 分钟（v1.x → v2.0 兼容垫片期可以 0 停机，详见 §3） |
| 回滚可用 | 30 分钟内可回滚到 v1.x（限制：v2.0 写入的数据可能丢） |

---

## 1. 三种升级路径

### 1.1 路径 A：原地升级（推荐 - 中小规模）

**适用**：
- DB 数据 ≤ 1M 行
- 可接受 30 分钟停机窗口
- 无跨地域复杂部署

**步骤概要**：

```
1. 进 maintenance mode（拒绝新任务）
2. 等 in-flight task 进入稳定状态（completed/failed/paused）
3. PG basebackup 全量备份
4. alembic upgrade（跑全部 migration）
5. 数据回填 script（默认 tenant_id=1）
6. 部署 v2.0 代码
7. 退出 maintenance mode
8. 监控 1h，无异常则关闭回滚窗口
```

**详见 §4 - 详细 runbook**

### 1.2 路径 B：兼容垫片（推荐 - 大规模 / 高 SLA）

**适用**：
- 不能接受停机
- 数据量大（≥ 5M 行）
- 多 cluster

**步骤概要**：

```
1. v2.0 代码加 v1.x API 兼容层（/api/v1/* 仍可读，仅创建/修改走 v2）
2. DB 加 v2.0 新列（默认值 NULL），不删 v1 列
3. 双写期：v2.0 代码同时写 v1 字段 + v2 字段
4. 后台任务批量回填 v2.0 字段
5. 切换读：v2.0 代码改读 v2 字段
6. 灰度部署（10% → 50% → 100%）
7. 一段时间（≥ 30 天）后删 v1 列
```

**详见 §5**

### 1.3 路径 C：并行迁移（仅 PoC 数据少时）

**适用**：
- 数据量极小（&lt; 1k 任务）或 PoC 阶段
- 可以放弃历史数据

**步骤**：直接搭 v2.0，导出 v1 任务列表手动重提交。

**不适用生产**。

---

## 2. Alembic Migration 序列

> 每个 migration 一个文件；每个 Phase 对应一组 migration。
> 文件命名：`YYYY_NN_<topic>.py`。

### 2.1 Phase 1 migrations（单租户基础）

```python
# alembic/versions/20260501_01_initial.py
"""Initial v2.0 schema (Phase 1 scope)"""

def upgrade():
    # tenants
    op.create_table('tenants',
        sa.Column('id', sa.BigInteger, primary_key=True),
        sa.Column('slug', sa.String(64), unique=True, nullable=False),
        sa.Column('display_name', sa.String(128), nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('quota_bytes_month', sa.BigInteger, server_default='0', nullable=False),
        sa.Column('quota_concurrent', sa.Integer, server_default='10', nullable=False),
        sa.Column('quota_storage_gb', sa.BigInteger, server_default='1024', nullable=False),
        sa.Column('is_active', sa.Boolean, server_default=sa.true(), nullable=False),
    )

    # default tenant for migration
    op.execute("INSERT INTO tenants (slug, display_name) VALUES ('default', 'Default Tenant')")

    # projects
    op.create_table('projects',
        sa.Column('id', sa.BigInteger, primary_key=True),
        sa.Column('tenant_id', sa.BigInteger, sa.ForeignKey('tenants.id'), nullable=False),
        sa.Column('name', sa.String(128), nullable=False),
        sa.Column('storage_id', sa.BigInteger, nullable=True),
        sa.UniqueConstraint('tenant_id', 'name'),
    )
    op.execute("INSERT INTO projects (tenant_id, name) "
               "SELECT id, 'default' FROM tenants WHERE slug='default'")

    # users
    op.create_table('users',
        sa.Column('id', sa.BigInteger, primary_key=True),
        sa.Column('tenant_id', sa.BigInteger, sa.ForeignKey('tenants.id'), nullable=False),
        sa.Column('oidc_subject', sa.String(256), unique=True, nullable=False),
        sa.Column('email', sa.String(256)),
        sa.Column('role', sa.String(32), nullable=False),
        sa.Column('is_active', sa.Boolean, server_default=sa.true(), nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
    )
    op.create_index('idx_users_tenant', 'users', ['tenant_id'])

    # storage_backends
    op.create_table('storage_backends',
        sa.Column('id', sa.BigInteger, primary_key=True),
        sa.Column('tenant_id', sa.BigInteger, sa.ForeignKey('tenants.id'), nullable=True),
        sa.Column('name', sa.String(128), nullable=False),
        sa.Column('backend_type', sa.String(32), nullable=False),
        sa.Column('region', sa.String(64)),
        sa.Column('config_encrypted', sa.LargeBinary, nullable=False),
        sa.Column('is_default', sa.Boolean, server_default=sa.false(), nullable=False),
        sa.UniqueConstraint('tenant_id', 'name'),
    )

    # download_tasks
    op.create_table('download_tasks',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text('gen_random_uuid()')),
        sa.Column('tenant_id', sa.BigInteger, sa.ForeignKey('tenants.id'), nullable=False),
        sa.Column('project_id', sa.BigInteger, sa.ForeignKey('projects.id'), nullable=False),
        sa.Column('owner_user_id', sa.BigInteger, sa.ForeignKey('users.id'), nullable=False),
        sa.Column('repo_id', sa.String(256), nullable=False),
        sa.Column('revision', sa.String(64), nullable=False),
        sa.Column('storage_id', sa.BigInteger, sa.ForeignKey('storage_backends.id'), nullable=False),
        sa.Column('path_template', sa.String(512), nullable=False),
        sa.Column('priority', sa.SmallInteger, server_default='1', nullable=False),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('is_simulation', sa.Boolean, server_default=sa.false(), nullable=False),
        sa.Column('download_bytes_limit', sa.BigInteger, nullable=True),
        sa.Column('upgrade_from_revision', sa.String(64), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('completed_at', sa.TIMESTAMP(timezone=True)),
        sa.Column('cancelled_at', sa.TIMESTAMP(timezone=True)),
        sa.Column('error_message', sa.Text),
        sa.Column('trace_id', sa.String(32)),
    )
    op.create_index('idx_tasks_tenant_status', 'download_tasks', ['tenant_id', 'status'])
    op.create_index('idx_tasks_dedup', 'download_tasks',
                     ['tenant_id', 'repo_id', 'revision'],
                     unique=True,
                     postgresql_where=sa.text("status NOT IN ('failed', 'cancelled')"))

    # file_subtasks
    op.create_table('file_subtasks',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text('gen_random_uuid()')),
        sa.Column('task_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('download_tasks.id', ondelete='CASCADE'), nullable=False),
        sa.Column('tenant_id', sa.BigInteger, nullable=False),
        sa.Column('filename', sa.String(512), nullable=False),
        sa.Column('file_size', sa.BigInteger),
        sa.Column('expected_sha256', sa.String(64)),
        sa.Column('actual_sha256', sa.String(64)),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('executor_id', sa.String(64)),
        # NOTE: epoch / token added in Phase 2
        sa.Column('chunks_total', sa.Integer),
        sa.Column('chunks_completed', sa.Integer, server_default='0', nullable=False),
        sa.Column('bytes_downloaded', sa.BigInteger, server_default='0', nullable=False),
        sa.Column('retry_count', sa.Integer, server_default='0', nullable=False),
        sa.Column('last_error', sa.Text),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('completed_at', sa.TIMESTAMP(timezone=True)),
        sa.UniqueConstraint('task_id', 'filename'),
    )
    op.create_index('idx_subtasks_status', 'file_subtasks', ['status', 'executor_id'])
    op.create_index('idx_subtasks_pending', 'file_subtasks', ['task_id'],
                     postgresql_where=sa.text("status = 'pending'"))

    # executors
    op.create_table('executors',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('tenant_id', sa.BigInteger, sa.ForeignKey('tenants.id'), nullable=True),
        sa.Column('host_id', sa.String(64), nullable=False),
        sa.Column('parent_executor_id', sa.String(64)),
        sa.Column('cert_fingerprint', sa.String(128)),  # NOTE: nullable in Phase 1
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('health_score', sa.SmallInteger, server_default='100', nullable=False),
        sa.Column('last_heartbeat_at', sa.TIMESTAMP(timezone=True)),
        sa.Column('consecutive_heartbeat_failures', sa.Integer, server_default='0', nullable=False),
        sa.Column('consecutive_task_failures', sa.Integer, server_default='0', nullable=False),
        sa.Column('degraded_failure_streak', sa.Integer, server_default='0', nullable=False),
        sa.Column('capabilities', postgresql.JSONB, server_default='{}', nullable=False),
        sa.Column('nic_speed_gbps', sa.SmallInteger),
        sa.Column('disk_free_gb', sa.BigInteger),
        sa.Column('disk_total_gb', sa.BigInteger),
        sa.Column('parts_dir_bytes', sa.BigInteger, server_default='0', nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('deactivated_at', sa.TIMESTAMP(timezone=True)),
    )
    op.create_index('idx_executors_status', 'executors', ['status', 'last_heartbeat_at'])

    # executor_status_history
    op.create_table('executor_status_history',
        sa.Column('id', sa.BigInteger, primary_key=True),
        sa.Column('executor_id', sa.String(64), nullable=False),
        sa.Column('from_status', sa.String(32), nullable=False),
        sa.Column('to_status', sa.String(32), nullable=False),
        sa.Column('reason', sa.String(64), nullable=False),
        sa.Column('occurred_at', sa.TIMESTAMP(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
    )

def downgrade():
    op.drop_table('executor_status_history')
    op.drop_table('executors')
    op.drop_index('idx_subtasks_pending')
    op.drop_index('idx_subtasks_status')
    op.drop_table('file_subtasks')
    op.drop_index('idx_tasks_dedup')
    op.drop_index('idx_tasks_tenant_status')
    op.drop_table('download_tasks')
    op.drop_table('storage_backends')
    op.drop_index('idx_users_tenant')
    op.drop_table('users')
    op.drop_table('projects')
    op.drop_table('tenants')
```

### 2.2 Phase 2 migrations（fence token + mTLS）

```python
# alembic/versions/20260601_01_fence_token.py
"""Phase 2: fence token + executor epoch + mTLS fields"""

def upgrade():
    # executors: epoch
    op.add_column('executors',
        sa.Column('epoch', sa.BigInteger, server_default='0', nullable=False))
    # cert_fingerprint 升级为 NOT NULL
    op.execute("UPDATE executors SET cert_fingerprint = 'pre-mtls-' || id "
               "WHERE cert_fingerprint IS NULL")
    op.alter_column('executors', 'cert_fingerprint', nullable=False)
    # enrollment_token_id（新外键，先允许 NULL）
    op.create_table('enrollment_tokens', ...)  # see schema
    op.add_column('executors',
        sa.Column('enrollment_token_id', sa.BigInteger,
                  sa.ForeignKey('enrollment_tokens.id'), nullable=True))

    # file_subtasks: fence token + multipart + actual_sha256
    op.add_column('file_subtasks',
        sa.Column('executor_epoch', sa.BigInteger, nullable=True))
    op.add_column('file_subtasks',
        sa.Column('assignment_token', postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column('file_subtasks',
        sa.Column('multipart_upload_id', sa.String(256), nullable=True))
    op.add_column('file_subtasks',
        sa.Column('multipart_started_at', sa.TIMESTAMP(timezone=True), nullable=True))

    # status 重命名: transferring -> uploading
    op.execute("UPDATE file_subtasks SET status = 'uploading' WHERE status = 'transferring'")

    # 状态约束（CHECK constraint）
    op.execute("""
        ALTER TABLE file_subtasks ADD CONSTRAINT subtask_status_valid
        CHECK (status IN (
            'pending', 'assigned', 'downloading',
            'verifying_local', 'uploading', 'verifying_remote', 'verified',
            'failed_permanent', 'paused_external', 'paused_disk_full',
            'cancelling', 'cancelled'
        ))
    """)

def downgrade():
    op.execute("ALTER TABLE file_subtasks DROP CONSTRAINT subtask_status_valid")
    op.execute("UPDATE file_subtasks SET status = 'transferring' WHERE status = 'uploading'")
    op.drop_column('file_subtasks', 'multipart_started_at')
    op.drop_column('file_subtasks', 'multipart_upload_id')
    op.drop_column('file_subtasks', 'assignment_token')
    op.drop_column('file_subtasks', 'executor_epoch')
    op.drop_column('executors', 'enrollment_token_id')
    op.drop_table('enrollment_tokens')
    op.drop_column('executors', 'epoch')
```

### 2.3 Phase 3 migrations（多租户 + 多源 + 配额）

```python
# alembic/versions/20260701_01_multi_source.py
"""Phase 3a: multi-source"""

def upgrade():
    op.add_column('download_tasks',
        sa.Column('source_strategy', sa.String(32),
                  server_default='auto_balance', nullable=False))
    op.add_column('download_tasks',
        sa.Column('source_blacklist', postgresql.JSONB,
                  server_default='[]', nullable=False))
    op.add_column('file_subtasks',
        sa.Column('source_id', sa.String(32), nullable=True))

    op.create_table('subtask_chunks', ...)
    op.create_table('source_speed_samples', ...)
    op.create_table('source_throttle_state', ...)
    op.create_table('file_fingerprints', ...)

# alembic/versions/20260701_02_quota.py
"""Phase 3b: quota & usage"""

def upgrade():
    op.create_table('usage_records', ...)
    op.create_table('quota_snapshots', ...)
    # 初始化所有租户 snapshot
    op.execute("INSERT INTO quota_snapshots (tenant_id) SELECT id FROM tenants")
```

### 2.4 Phase 4 migrations（合规 + 审计）

```python
# alembic/versions/20260801_01_audit.py
"""Phase 4a: audit log with tamper-evident chain"""

def upgrade():
    op.create_table('audit_log',
        sa.Column('id', sa.BigInteger, primary_key=True),
        sa.Column('occurred_at', sa.TIMESTAMP(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('tenant_id', sa.BigInteger, nullable=True),
        sa.Column('actor_user_id', sa.BigInteger, nullable=True),
        sa.Column('actor_ip', postgresql.INET),
        sa.Column('action', sa.String(64), nullable=False),
        sa.Column('resource_type', sa.String(32), nullable=False),
        sa.Column('resource_id', sa.String(128)),
        sa.Column('outcome', sa.String(16), nullable=False),
        sa.Column('payload', postgresql.JSONB),
        sa.Column('trace_id', sa.String(32)),
        sa.Column('prev_hash', sa.String(64)),
        sa.Column('self_hash', sa.String(64), nullable=False),
    )
    op.create_index('idx_audit_actor', 'audit_log', ['actor_user_id', 'occurred_at'])
    op.create_index('idx_audit_resource', 'audit_log', ['resource_type', 'resource_id'])
    op.create_index('idx_audit_action_time', 'audit_log', ['action', 'occurred_at'])

    # GENESIS row
    op.execute("""
        INSERT INTO audit_log (action, resource_type, outcome, prev_hash, self_hash)
        VALUES ('audit.genesis', 'system', 'success',
                '0000000000000000000000000000000000000000000000000000000000000000',
                'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855')
    """)

# alembic/versions/20260801_02_license_policy.py
"""Phase 4b: license policy + gated approvals"""

def upgrade():
    op.create_table('license_policies', ...)
    op.create_table('gated_model_approvals', ...)
```

---

## 3. 数据回填策略

### 3.1 v1.x 数据 → v2.0 字段映射

```python
# scripts/migration/backfill_v2.py

def backfill_phase1():
    """Phase 1: 把 v1.x 任务塞到 default tenant"""
    default_tenant = get_or_create_tenant("default", "Default Tenant")
    default_project = get_or_create_project(default_tenant.id, "default")
    system_user = get_or_create_user(default_tenant.id, "system@local", "tenant_admin")
    default_storage = get_or_create_storage(default_tenant.id, name="legacy")

    # 已存在的 v1.x download_tasks 表数据迁移
    db.execute("""
        UPDATE download_tasks
        SET tenant_id = :t,
            project_id = :p,
            owner_user_id = :u,
            storage_id = :s,
            path_template = '{tenant}/' || repo_id || '/' || COALESCE(revision, 'main')
        WHERE tenant_id IS NULL
    """, t=default_tenant.id, p=default_project.id, u=system_user.id, s=default_storage.id)

    # status 规范化
    db.execute("""
        UPDATE download_tasks SET status = 'pending'
        WHERE status NOT IN ('pending', 'scheduling', 'downloading', 'verifying',
                              'completed', 'failed', 'cancelling', 'cancelled')
    """)

def backfill_phase2():
    """Phase 2: epoch / fence_token 默认值"""
    db.execute("UPDATE executors SET epoch = 1 WHERE epoch = 0")
    # subtask 仍处于 in-flight 状态的：清空 token 让其被重新分配
    db.execute("""
        UPDATE file_subtasks
        SET status = 'pending', executor_id = NULL,
            executor_epoch = NULL, assignment_token = NULL
        WHERE status IN ('assigned', 'downloading') AND executor_epoch IS NULL
    """)

def backfill_phase3():
    """Phase 3: 计算 quota_snapshots"""
    db.execute("""
        WITH usage AS (
            SELECT tenant_id, COALESCE(SUM(bytes_downloaded), 0) AS used
            FROM file_subtasks WHERE created_at >= date_trunc('month', now())
            GROUP BY tenant_id
        )
        UPDATE quota_snapshots q
        SET bytes_used_month = u.used
        FROM usage u
        WHERE q.tenant_id = u.tenant_id
    """)
    # storage_objects refcount
    populate_storage_objects_from_completed_subtasks()

def backfill_phase4():
    """Phase 4: 审计日志补录关键历史事件"""
    # 不补录历史用户行为（不可信）；仅写一条 'migration.completed' 事件
    audit_log("migration.v2.0_completed", "system", "success", actor=None)
```

### 3.2 大表回填技巧（避免长事务）

```python
def backfill_in_batches(table, batch_size=10000):
    """游标分页 + 单事务限定 batch_size 行"""
    last_id = 0
    while True:
        with db.transaction():
            ids = db.execute(f"""
                SELECT id FROM {table}
                WHERE id > :last AND tenant_id IS NULL
                ORDER BY id LIMIT :batch
            """, last=last_id, batch=batch_size).fetchall()
            if not ids:
                break
            db.execute(f"""
                UPDATE {table} SET tenant_id = 1
                WHERE id IN ({','.join(str(r.id) for r in ids)})
            """)
            last_id = ids[-1].id
        time.sleep(0.1)  # 避免长时间锁
```

### 3.3 回填顺序（受外键约束）

```
1. tenants（先 default tenant）
2. projects（先 default project）
3. users（先 system user）
4. storage_backends
5. download_tasks（依赖前 4 个）
6. file_subtasks（依赖 download_tasks）
7. executors
8. usage_records / quota_snapshots（依赖 tenants）
9. audit_log（最后写一条 migration.completed）
```

---

## 4. 路径 A 详细 Runbook（原地升级）

### 4.1 Pre-flight（升级前 1 周）

- [ ] 在 staging 上跑完全部迁移流程，记录耗时
- [ ] 备份 production PG（pg_basebackup）
- [ ] 通知所有租户 / 用户：维护窗口
- [ ] 运维 on-call 排班
- [ ] 准备 v1.x 镜像 tag（用于回滚）
- [ ] 准备回滚脚本（已测试）
- [ ] 准备 v2.0 镜像 + 配置

### 4.2 升级窗口（30 min）

```bash
# T+0:00  进 maintenance mode（v1.x）
$ ./scripts/v1/maintenance enter

# T+0:01  等 in-flight 任务进入稳定状态（最多 5min）
$ ./scripts/v1/wait-stable.sh --timeout 300

# T+0:06  pg_basebackup 全量备份
$ pg_basebackup -D /backup/pre-v2.0-$(date +%s) -F tar -z -P

# T+0:10  alembic upgrade（多个 migration 顺序跑）
$ alembic upgrade head
# 输出：耗时 ~5min for 1M subtask（含索引）

# T+0:15  数据回填
$ python scripts/migration/backfill_v2.py --phase 1

# T+0:18  部署 v2.0 镜像
$ kubectl set image deployment/dlw-controller controller=dlw:v2.0.0
$ kubectl set image deployment/dlw-executor executor=dlw:v2.0.0
$ kubectl rollout status deployment/dlw-controller
$ kubectl rollout status deployment/dlw-executor

# T+0:23  smoke test
$ ./scripts/v2/smoke-test.sh
# 验证：API 200、心跳通、新任务可创建

# T+0:25  退出 maintenance mode
$ ./scripts/v2/maintenance exit

# T+0:26  监控仪表板观察 1h
# - 任务完成率
# - API 错误率
# - 心跳成功率
```

### 4.3 升级后验证（T+1h）

- [ ] 创建一个 test 任务，跑通 E2E
- [ ] 抽 5 个 v1.x 完成的任务，验证 storage 文件仍可访问
- [ ] 检查 v1.x 进行中的任务是否 resume
- [ ] 运行 `pytest tests/migration_smoke/`

### 4.4 回滚预案

**触发条件**：
- 升级后 1h 内 API 错误率 ≥ 5%
- 升级后任何 P0 告警
- 数据完整性怀疑（任务状态混乱、文件丢失）

**回滚步骤**：

```bash
# 决定回滚后立即：
# T+0  进 maintenance
$ ./scripts/v2/maintenance enter

# T+1  alembic downgrade
$ alembic downgrade -3   # 回退最近 3 个 migration（Phase 1 全部）

# T+5  恢复 v1.x 镜像
$ kubectl set image deployment/dlw-controller controller=dlw:v1.5.0

# T+8  退出 maintenance
$ ./scripts/v1/maintenance exit
```

⚠️ **回滚限制**：
- v2.0 期间创建的任务会丢失（v1.x schema 不兼容新字段）
- 已完成任务的 storage 文件保留
- 审计日志在 v1.x 下不可读（v1.x 无此表）

---

## 5. 路径 B 详细策略（兼容垫片，0 停机）

### 5.1 阶段划分

```
Stage 1（1 周）:  双写期 准备
  - 部署 v2.0 代码（含 v1.x 兼容路由）
  - DB 加 v2.0 新列（NULL 默认）
  - 应用代码：写时双写 v1+v2 字段

Stage 2（持续 ≥ 1 周）: 双写期
  - 后台批量回填历史数据（backfill_in_batches）
  - 监控数据一致性（v1 字段与 v2 字段是否同步）

Stage 3（1 周）: 切读
  - 应用代码改读 v2 字段
  - v1 字段仍写但不读
  - 灰度 10% → 50% → 100%

Stage 4（≥ 30 天后）: 清理
  - 应用代码删除 v1 字段写入
  - alembic 删除 v1 字段
```

### 5.2 兼容路由层

```python
# v1.x API 兼容
@app.get("/api/v1/tasks/{task_id}")
async def get_task_v1(task_id: str):
    task = await db.get_task(task_id)
    return {
        "id": task.id,
        "repo_id": task.repo_id,
        "revision": task.revision,
        # v2.0 新字段不暴露
        "target_dir": resolve_v1_target_dir(task),  # 由 storage_id+path_template 反推
        "status": task.status,
    }

@app.post("/api/v1/tasks")
async def create_task_v1(req: CreateTaskV1Request):
    # v1.x 客户端不知道 tenant_id；用调用者 token 推断
    user = await auth_user(request)
    return await create_task_v2(
        repo_id=req.repo_id,
        revision=resolve_revision(req.revision),  # main → sha
        storage_id=user.tenant.default_storage_id,
        path_template=req.target_dir,
        owner_user_id=user.id,
    )
```

### 5.3 双写应用代码

```python
class TaskService:
    async def update_status(self, task_id, new_status):
        # v2.0 写
        await db.execute("UPDATE download_tasks SET status = :s WHERE id = :id ...")

        # 双写期：v1.x 字段也写
        if FEATURE_FLAGS.dual_write_v1:
            v1_status = map_v2_to_v1_status(new_status)
            await db.execute("UPDATE legacy_tasks SET status = :s ...")
```

---

## 6. 数据完整性检查

### 6.1 升级前

```sql
-- 任务计数
SELECT count(*), status FROM download_tasks GROUP BY status;
-- 文件计数
SELECT count(*) FROM file_subtasks;
-- 存储中文件数（与 DB 比对）
$ aws s3 ls s3://bucket/ --recursive | wc -l
```

### 6.2 升级后

```sql
-- 任务计数无变化
SELECT count(*) FROM download_tasks;  -- 应等于升级前

-- 所有任务有 tenant_id
SELECT count(*) FROM download_tasks WHERE tenant_id IS NULL;  -- 应 = 0

-- 状态机合法
SELECT status, count(*) FROM file_subtasks GROUP BY status;
SELECT status, count(*) FROM download_tasks GROUP BY status;
-- 不应该有 transferring 状态

-- 不变量 8（业务表 tenant_id）
SELECT table_name FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name IN ('download_tasks', 'file_subtasks', 'executors', 'storage_backends')
  AND NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = t.table_name AND column_name = 'tenant_id'
  );
-- 应 = 0 行

-- Phase 4：审计链
SELECT count(*) FROM audit_log WHERE prev_hash IS NULL AND id > 1;  -- 应 = 0（只有 genesis）
```

---

## 7. 灰度部署

仅适用于多 cluster 部署：

```
1. 在 1 个 dev cluster 上完整跑 Phase 1-4
2. 在 staging cluster 跑 Phase 1（仅）观察 1 周
3. 在 1 个 prod cluster 跑 Phase 1
4. 监控 1 周稳定 → 其他 prod cluster
5. 重复 Phase 2 / 3 / 4
```

---

## 8. 升级失败的处置

### 8.1 alembic upgrade 失败

```
- 立即 alembic downgrade
- 检查 PG 错误日志
- 如果是单条数据问题：手动修复后重跑
- 如果是索引问题：手动 CREATE INDEX CONCURRENTLY
```

### 8.2 数据不一致

```
- 比对 backup 与当前数据
- 用 backup 重建受影响表（DROP TABLE + restore + alembic upgrade 该表）
- 如果是 sha 不匹配类问题：从 storage 重新计算 sha 比对
```

### 8.3 应用启动失败

```
- 立即回退镜像
- 不动 DB
- 检查应用日志
- 修复后重新部署
```

---

## 9. 升级 checklist

### 9.1 Phase 升级 checklist 模板

```markdown
## Pre-flight
- [ ] Staging 演练完成
- [ ] Backup 已完成（路径：______）
- [ ] 用户通知已发出
- [ ] Runbook 已 review
- [ ] 回滚脚本已测试
- [ ] On-call 已就位

## During
- [ ] Maintenance mode 进入
- [ ] In-flight 已稳定
- [ ] alembic upgrade 完成
- [ ] 数据回填完成
- [ ] 应用部署完成
- [ ] Smoke test 通过

## Post
- [ ] Maintenance mode 退出
- [ ] 1h 监控通过
- [ ] 数据完整性 SQL 通过
- [ ] 用户公告（升级完成）
- [ ] 复盘文档归档
```

---

## 10. 与其他文档的链接

- 数据模型：→ [01-architecture.md](./01-architecture.md) §4
- Phase 计划：→ [08-mvp-roadmap.md](./08-mvp-roadmap.md)
- 测试矩阵：→ [07-test-plan.md](./07-test-plan.md) §3.5（升级测试）
- Runbook：→ [05-operations.md](./05-operations.md) §4
