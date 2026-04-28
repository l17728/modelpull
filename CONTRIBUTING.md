# 贡献指南

感谢你对 **modelpull** 感兴趣！本文档说明在不同阶段如何贡献。

> ⚠️ **当前阶段：设计完成 · 代码未实现**。此阶段最有价值的贡献是**设计 review** 和**文档/规范修订**。代码贡献请等 Phase 1 启动后。

---

## 目录

- [贡献类型](#贡献类型)
- [开始之前](#开始之前)
- [设计 Review（当前阶段最重要）](#设计-review当前阶段最重要)
- [文档 / 规范修订](#文档--规范修订)
- [部署物料修订](#部署物料修订)
- [代码贡献（Phase 1+）](#代码贡献phase-1)
- [PR 流程](#pr-流程)
- [Commit 规范](#commit-规范)
- [Code of Conduct](#code-of-conduct)

---

## 贡献类型

| 类型 | Issue 模板 | 当前阶段优先级 |
|------|----------|--------------|
| 🏛 设计 review（架构 / 不变量 / 协议） | `Design Review` | ⭐⭐⭐ 最高 |
| 🐛 文档矛盾 / 规范错误 / 部署物料 bug | `Bug Report` | ⭐⭐ |
| ✨ 新 feature 提议 | `Feature Request` | ⭐ 进 v2.1+ roadmap |
| 💬 提问 / 讨论 | GitHub Discussions | 随时 |
| 💻 代码（Python 后端 / Vue 前端 / SDK） | （Phase 1 启动后开放） | 暂不接受 |

---

## 开始之前

1. **读完 [`docs/v2.0/00-INDEX.md`](./docs/v2.0/00-INDEX.md)** — 不同角色有不同推荐路径
2. **了解 [14 条核心不变量](./docs/v2.0/01-architecture.md)（§7）** — 这是项目的"宪法"
3. **了解 [4-Phase MVP 路线图](./docs/v2.0/08-mvp-roadmap.md)** — 知道当前在哪个阶段
4. 搜索现有 [Issues](https://github.com/l17728/modelpull/issues) 与 [Discussions](https://github.com/l17728/modelpull/discussions) 避免重复

---

## 设计 Review（当前阶段最重要）

设计阶段是修正架构错误成本最低的窗口。我们格外欢迎严格 review。

### 5 位 Reviewer 视角（任选其一）

1. **架构一致性** — 跨文档矛盾 / 抽象边界 / 依赖分层 / 可演进性
2. **分布式正确性** — 并发竞态 / 状态机完备性 / crash-consistency / 故障注入下的语义
3. **安全合规** — 认证、凭证管理、网络面、供应链、DoS、审计、合规
4. **运维可观测** — SLO / 告警 / 容量 / 成本 / Runbook / 灰度 / 备份 / 降级
5. **用户价值盲区** — 多租户 / 配额 / 生态集成 / 合规 / UX

### 怎样的 review 最有价值

✅ **好的 review**：
- 引用具体文件 + 行号 + 引文
- 给出可证伪的触发场景（推荐 Lamport 风格事件序列）
- 判断严重等级（🔴 Critical / 🟡 High / 🟢 Medium）
- 提出**具体**的修复建议，不是"应该加身份认证"这种空话

❌ **不太有用的 review**：
- "我觉得这个架构不行" + 没有具体证据
- "为什么不用 X 框架" + 没有对比当前选型
- "和 Spanner 一样设计就好了" + 没有指明哪些原则适用此场景

### 提交方式

打开 [Design Review issue](https://github.com/l17728/modelpull/issues/new?template=design_review.yml)。

---

## 文档 / 规范修订

### 流程

1. Fork 仓库
2. 创建分支：`docs/<short-topic>` 或 `fix/<topic>`
3. 修改对应文档
4. 自审 4 项：
   - 占位扫描（`TODO` / `TBD` / `FIXME`）
   - 跨文档引用一致性（markdown link 不破坏）
   - 内部矛盾（同一概念不能在两处定义不同）
   - 不变量影响（如有，PR 描述需明确）
5. 提交 PR

### 文档维护原则

- **数据模型**仅在 `01-architecture.md` §4 与 `02-protocol.md` §2 OpenAPI 定义；其他文档只引用
- **状态机**仅在 `01-architecture.md` §3 定义；其他文档只引用，不重画
- **不变量**仅在 `01-architecture.md` §7 索引；新增不变量必须更新此索引
- **跨文档**用相对链接（`./05-operations.md` 而非绝对 URL）
- **修改日志**记在 `00-INDEX.md` 末尾（不在章节 inline）

---

## 部署物料修订

涉及 `deploy/` 目录的修改前请本地验证：

```bash
# Helm
helm lint deploy/helm/
helm template dlw deploy/helm/ > /tmp/rendered.yaml
kubeconform -strict /tmp/rendered.yaml      # 需安装 kubeconform

# Shell scripts
shellcheck deploy/runbooks/scripts/*.sh

# YAML
yamllint deploy/ api/

# JSON (Grafana)
python3 -c "import json; json.load(open('deploy/grafana/overview-dashboard.json'))"
```

CI 会跑同样的检查；本地通过更省时间。

---

## 代码贡献（Phase 1+）

Phase 1 启动后将开放代码贡献。届时本节会更新具体规范，含：

- 项目骨架 / 依赖管理（uv / pnpm）
- 测试覆盖率门槛（详见 [`07-test-plan.md`](./docs/v2.0/07-test-plan.md)）
- 代码风格（black + ruff + mypy strict / ESLint + Prettier）
- 不变量必须断言（CI 强制）

预计 Phase 1 启动时间：见 [`08-mvp-roadmap.md`](./docs/v2.0/08-mvp-roadmap.md)。

---

## PR 流程

1. **小步快跑**：单 PR 不超过 500 行 diff（设计 PR 可以更大，但请按主题切分）
2. **PR 标题**：`[area] 简短描述`，例如：
   - `[docs] fix state machine inconsistency in 01 §3.2`
   - `[helm] add NetworkPolicy for executor egress`
   - `[ci] add markdownlint to PR check`
3. **PR 描述**：使用模板（自动加载），关键项：
   - 引用 issue：`Fixes #N` / `Refs #N`
   - 影响范围
   - 是否影响不变量（必须明确说！）
   - 验证方式
4. **CI 必须全绿**才会被 review
5. **Squash merge**：保持 main 历史整洁
6. **Review 时间**：通常 1-3 个工作日内首次响应

### 大变更（涉及不变量 / 协议）的额外要求

- 先开 [Design Review issue](https://github.com/l17728/modelpull/issues/new?template=design_review.yml) 讨论
- 至少 1 位维护者明确同意架构方向后再提 PR
- PR 中必须更新：`01 §7` 不变量索引 + 涉及的章节 + `00-INDEX.md` 修改日志

---

## Commit 规范

格式：`[area] <imperative summary>`（首字母小写，无句号）

例：

- `[docs] clarify fence token issuance in 03 §2.3`
- `[helm] reduce executor terminationGracePeriodSeconds to 720`
- `[ci] add lychee link check`
- `[fix] correct typo in 06 §1.6 LPT pseudocode`

如果 commit body 需要多行：

```
[docs] fix state machine drift between 01 and 06

The transferring state was already removed in 01 §3 but 06 §7 still
listed it as a valid status. This PR removes the reference and points
to the canonical state machine definition.

Refs #42
```

不接受：
- `wip`
- `update`
- `fix bug`（哪个 bug？）
- emoji 开头（CI 跑 commit linter）

---

## Code of Conduct

简单版：

- **尊重他人**：批评设计，不批评人。"This design is wrong because X" ✓ ；"You're stupid for designing this" ✗
- **承担解释责任**：你的 review/PR 是要别人花时间看的，写清楚比写多重要
- **跨语言友好**：项目主要语言为中文，但 issue / PR / 代码注释都接受中英双语；避免方言或缩写
- **不发暴力 / 性别歧视 / 种族歧视内容**

违反者：先警告 → 临时封禁 → 永久封禁。维护者保留最终解释权。

---

## 致谢

每位贡献者都会被列入 [Contributors](https://github.com/l17728/modelpull/graphs/contributors)。

特别感谢：

- 设计阶段提供 review 的 5 位虚拟 reviewer 视角（启发了 70+ 条问题修复）
- HuggingFace 团队的 Hub API 与 huggingface_hub SDK
- ModelScope（魔搭）社区的国内镜像
- hf-mirror.com 维护者

---

如有任何疑问，欢迎在 [Discussions](https://github.com/l17728/modelpull/discussions) 提问。
