# Getting Started

> **Status**: 📐 Design only — no executable code yet.
>
> 这份文档**不会**让你跑通"下载第一个模型"——因为代码尚未实现。
> 它会告诉你：**现在能做什么 / Phase 1 启动后怎么开始**。

---

## 1. 你来对地方了吗？

回答这 3 个问题：

| 问题 | 答案 = "是" 你应该 |
|------|-----------------|
| 我想用一个**能跑**的 HF 多机下载工具 | ❌ 不要看 modelpull；看 [`huggingface_hub.snapshot_download`](https://huggingface.co/docs/huggingface_hub) |
| 我对**分布式系统设计**感兴趣，想 review 28000 行真实设计 | ✅ 进 §2 |
| 我是**潜在用户**，想了解 modelpull 将来能做什么 | ✅ 读 [README](../README.md) → ROADMAP |
| 我想**贡献代码** | ⏳ 等 Phase 1 启动；现在可以提 design review issue |

---

## 2. 现在你能做的 5 件事

### 2.1 读设计入口

```
docs/v2.0/00-INDEX.md
```

按你的角色挑 1 条阅读路径（5 条角色路径定义在 INDEX 里）。预计阅读量：

| 角色 | 阅读量 | 预计时间 |
|------|------|--------|
| PM / Tech Lead | 仅 08-mvp-roadmap | 1 小时 |
| QA | 07-test-plan + 状态机章节 | 3 小时 |
| Architect 全量 | 14 章 ~9500 行 | 1-2 工作日 |

### 2.2 提 Design Review Issue（最有价值）

[Design Review Template](https://github.com/l17728/modelpull/issues/new?template=design_review.yml)

提之前请：
- 引用具体行号（设计 review 模板要求 file:line）
- 指明严重等级（🔴 / 🟡 / 🟢）
- 给可证伪的 trigger 场景

### 2.3 看现有 Review 的发现

`docs/v2.0/00-INDEX.md` 末尾的「修改日志」记录了 3 轮 review 已发现的问题。
你可以：
- 看是否你想报的问题已经在列
- 看是否之前的修复有漏

### 2.4 跑 CI invariant lint（dev 上手的最小一步）

```bash
git clone https://github.com/l17728/modelpull
cd modelpull
pip install pytest                    # 仅依赖
python tools/lint_invariants.py        # 验证 46 不变量索引一致
python -m pytest tools/test_lint_invariants.py -v   # 9 个单测
```

期望输出：
```
OK: 46 invariants declared, 46 indexed in 01 §7, contiguous
9 passed in 0.7s
```

如果输出不一致 → 报告 `bug` issue。这是你贡献的第一个 PR 的 sandbox。

### 2.5 浏览 OpenAPI（如果你是前端 / SDK 开发者）

```bash
# 用 redocly CLI（npm 全局）
npx @redocly/cli preview-docs api/openapi.yaml
# → 浏览器打开 http://localhost:8080
```

或用 GitHub Pages 浏览（一旦部署完成）：`https://l17728.github.io/modelpull/api/`

---

## 3. Phase 1 启动后的 first-run 体验（设计中）

这一节是**未来的体验设计**。当前不能执行；用作 review 参考。

### 3.1 安装（设计中）

```bash
# 选项 A: pip
pip install dlw-cli      # ⚠️ 尚未发布到 PyPI

# 选项 B: docker-compose dev（Phase 1 Week 5 提供）
git clone https://github.com/l17728/modelpull
cd modelpull
docker-compose -f deploy/dev/docker-compose.yml up -d
```

### 3.2 第一次登录（设计中）

```bash
dlw login --server http://localhost:8000   # OIDC PKCE flow，浏览器自动打开
# 设计阶段：dev profile 内置 dummy IdP；prod 需要先做 OIDC 配置（详见 docs/operator/oidc-setup.md）
```

### 3.3 提交第一个任务（设计中）

```bash
dlw submit Qwen/Qwen3-7B-Instruct
# → Created task: uuid-xxx
# → Estimated: 14 GB / 8 files
# → Speed probe ETA: 5s
# Watch progress:
dlw watch <task-uuid>
```

### 3.4 Top 5 命令 cheatsheet（设计中）

```bash
dlw submit <repo>          # 创建任务
dlw watch <id>             # 实时跟随
dlw list --status running  # 看正在跑的
dlw cancel <id>            # 取消
dlw retry <id>             # 重试失败子任务
```

---

## 4. 进入 contributor 通道

如果你想写代码（Phase 1 启动后）：

1. 读 `CONTRIBUTING.md`
2. 看 [ROADMAP](../ROADMAP.md) 当前 Phase
3. 跑 [`docs/contributor/local-setup.md`](./contributor/local-setup.md) — local dev 环境

---

## 5. 常见问题

**Q: 为什么版本号 v2.0.13 但说"代码未启动"？**
A: v2.0.X 是**设计文档版本**，不是软件 release。软件版本（如 v2.0.0-alpha）将在 Phase 1 完成后开始。

**Q: Issue 数 = 0 是项目放弃了吗？**
A: 不是。这是因为我们在写设计，不是求 bug 报告。当前阶段最有价值的贡献是 design review。

**Q: 14 章太多，我从哪开始？**
A: 看 INDEX 中你的角色路径。如果纠结，从 `01-architecture.md` 开始（约 700 行，1-1.5 小时）。

**Q: 我能用 modelpull 当作面试 / 学习材料吗？**
A: 可以。架构 / fence token / 多源调度 / AI 安全这几章是相对独立的设计案例。

**Q: 商用许可？**
A: Apache 2.0。
