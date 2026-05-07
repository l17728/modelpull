<!-- PR 标题：[area] 简短描述。例：[docs] fix state machine inconsistency in 01 §3.2 -->

## 变更说明

<!-- 1-3 句说明动机和效果 -->

## 核心 Checklist (5 必勾)

- [ ] 引用相关 issue（`Fixes #` / `Refs #`）— 或在 PR 描述说明无关联
- [ ] CI 全绿（包括 invariant_lint）
- [ ] 不引入 `TODO/TBD/FIXME` 等占位
- [ ] 跨文档引用一致（无破坏的 markdown link）
- [ ] **如果改动了不变量（01 §7）：在 PR 描述中明确列出新增/修改/删除的 ID**

<details>
<summary><strong>展开：DB schema 变更声明 + 详细 checklist（仅当涉及时勾）</strong></summary>

### DB schema 变更声明（X-DOC-16）

涉及表 / 列变更必须填，否则不批准合并：

- 新增表：（无 / 列举）
- ALTER 列：（无 / 列举）
- `01 §4.7` 已同步：☐
- `09-migration.md` 已加 alembic migration：☐
- `INVARIANTS.md` 与 `GLOSSARY.md` 已检查（如涉及新概念）：☐

### 详细 Checklist

- [ ] 已自审 `00-INDEX.md` 修改日志
- [ ] 涉及 OpenAPI：YAML 通过 spectral lint
- [ ] 涉及 Helm：本地 `helm lint` + `helm template` 通过
- [ ] 涉及 shell：`shellcheck` 通过
- [ ] 涉及 SQL：本地 alembic upgrade head + downgrade -1 通过
- [ ] 涉及前端：`npm run lint` + `pnpm test` 通过
- [ ] 涉及不变量：在 `INVARIANTS.md` 加 Depends-on / Anti-example

</details>

## 测试 / 验证

<!--
- 文档 / 规范变更：说明 reviewer 该如何验证（例 "对照 03 §2 与 04 §3.1"）
- 部署物料变更：贴 helm lint 输出或截图
- 代码变更：列出新增 / 修改的测试 ID
-->

## 截图（可选）

---

🤖 _modelpull 当前处于设计阶段；PR 多为文档/规范修订。代码 PR 在 Phase 1 启动后接受。_
