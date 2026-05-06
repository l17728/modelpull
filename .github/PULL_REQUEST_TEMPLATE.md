<!-- PR 标题格式：[area] 简短描述。例：[docs] fix state machine inconsistency in 01 §3.2 -->

## 变更说明

<!-- 用 1-3 句说明动机和效果 -->

## 影响范围

- [ ] 设计文档（docs/v2.0/）
- [ ] OpenAPI（api/openapi.yaml）
- [ ] 部署物料（deploy/）
- [ ] CI / Issue 模板（.github/）
- [ ] 历史归档（docs/archive/）

## 核对

- [ ] 引用了相关 issue（`Fixes #` / `Refs #`）
- [ ] 已自审 `00-INDEX.md` 的"修改日志"，必要时已更新
- [ ] 跨文档引用一致（无破坏的 markdown link）
- [ ] 不引入新的占位（TODO/TBD/FIXME）
- [ ] 涉及 OpenAPI 时：YAML 语法通过 CI lint
- [ ] 涉及 Helm 时：`helm template` / `helm lint` 本地通过
- [ ] 涉及 shell 时：`shellcheck` 通过
- [ ] **如果改动了不变量（01 §7），已在 PR 描述中明确列出**
- [ ] **🚨 涉及 DB schema 变更（CREATE TABLE / ALTER TABLE / 新列）时：必须更新 `01-architecture.md §4` 权威数据模型；CI `invariant_lint` 通过**（X-DOC-16 强制）

## DB schema 变更声明（如适用）

<!-- 涉及表/列变更必须填，否则不批准合并 -->

- 新增表：（无 / 列举）
- ALTER 列：（无 / 列举）
- `01 §4.7` 已同步：☐
- `09-migration.md` 已加 alembic migration：☐

## 测试 / 验证

<!--
对文档/规范变更：说明 reviewer 该如何验证（例如"对照 03 §2 与 04 §3.1"）
对部署物料变更：贴 helm lint 输出或截图
-->

## 截图（可选）

---

🤖 _Note: modelpull 处于设计阶段，PR 多为文档/规范修订。代码 PR 在 Phase 1 启动后接受。_
