# 10 — 前端 Wireframe / 组件 / 状态管理

> 角色：前端工程师 / UI 设计师对齐"画什么、怎么画、状态怎么流"。
> 范围：技术选型、9 个核心页面 wireframe（ASCII mockup）、组件库、状态管理、实时数据流。

---

## 0. 技术选型

| 维度 | 选型 | 理由 |
|------|------|------|
| 框架 | **Vue 3 + Composition API + `<script setup>`** | TS 友好；composables 易复用；社区活 |
| 构建 | **Vite 5** | HMR 快、配置简单 |
| 语言 | **TypeScript 5.x，strict mode** | 与 OpenAPI 生成的 client 一致 |
| 路由 | **vue-router 4** | 默认 |
| 状态管理 | **Pinia 2** | Vue 3 官方推荐，TS 一等公民 |
| 数据获取 | **vue-query (@tanstack/vue-query)** | 缓存、自动刷新、乐观更新 |
| WebSocket | **自研 composable**（基于原生 WebSocket，含 snapshot+delta+seq 协议） | 见 §6 |
| UI 组件库 | **Element Plus 2.x** | 中文友好、表格/表单成熟、与设计风格匹配 |
| 图表 | **ECharts 5（vue-echarts wrapper）** | 实时图表性能 |
| 国际化 | **vue-i18n 9** | 中英双语 |
| OIDC | **oidc-client-ts** | PKCE flow + silent refresh |
| API client | **生成自 `api/openapi.yaml`**（用 `openapi-typescript-codegen`） | 单一真相 |
| 测试 | **Vitest + Playwright** | unit + E2E |
| Lint | **ESLint + Prettier + Vue 官方 plugin** | CI 强制 |
| 包管理 | **pnpm** | monorepo 友好 |

📝 **决策记录**：

- **不用 Nuxt SSR**：本系统是 SPA + WebSocket，SSR 收益有限
- **不用 Tailwind 单独**：Element Plus 已自带设计 token，混用会冲突
- **不用 Quasar / Vuetify**：Element Plus 在中文 admin 后台生态最强

---

## 1. 项目结构

```
frontend/
├── package.json
├── vite.config.ts
├── tsconfig.json
├── .eslintrc.cjs
├── index.html
├── public/
│   └── favicon.svg
├── src/
│   ├── main.ts                          # 入口
│   ├── App.vue
│   ├── api/                             # OpenAPI 生成的 client
│   │   ├── generated/                   # 自动生成，git ignore
│   │   ├── client.ts                    # 配置 axios + auth interceptor
│   │   └── ws.ts                        # WebSocket 协议封装
│   ├── auth/                            # OIDC
│   │   ├── oidc.ts
│   │   ├── guard.ts                     # router guard
│   │   └── store.ts                     # auth store
│   ├── stores/                          # Pinia stores
│   │   ├── tasks.ts
│   │   ├── executors.ts
│   │   ├── tenants.ts
│   │   ├── progress.ts                  # WS 实时进度
│   │   └── ui.ts                        # 主题、菜单收起等
│   ├── composables/
│   │   ├── useTaskList.ts
│   │   ├── useTaskDetail.ts
│   │   ├── useWebSocket.ts              # snapshot+delta+seq 协议
│   │   ├── useSpeedProbe.ts
│   │   └── useQuota.ts
│   ├── components/
│   │   ├── common/                      # Button、Confirm、Empty 等
│   │   ├── layout/
│   │   │   ├── AppLayout.vue
│   │   │   ├── SideNav.vue
│   │   │   └── TopBar.vue
│   │   ├── task/
│   │   │   ├── TaskList.vue
│   │   │   ├── TaskCard.vue
│   │   │   ├── TaskCreateForm.vue
│   │   │   ├── TaskProgressRing.vue
│   │   │   ├── FileMatrix.vue           # 163 文件 4 列网格
│   │   │   └── SourceAllocationView.vue
│   │   ├── executor/
│   │   │   ├── ExecutorList.vue
│   │   │   ├── HostGroup.vue
│   │   │   └── HealthBadge.vue
│   │   └── chart/
│   │       ├── SpeedChart.vue
│   │       └── SourceComboChart.vue
│   ├── pages/                           # 路由
│   │   ├── Dashboard.vue
│   │   ├── tasks/
│   │   │   ├── List.vue
│   │   │   ├── Detail.vue
│   │   │   └── Create.vue
│   │   ├── executors/
│   │   │   ├── List.vue
│   │   │   └── Detail.vue
│   │   ├── search/
│   │   │   └── ModelSearch.vue
│   │   ├── quota/
│   │   │   └── Quota.vue
│   │   ├── audit/
│   │   │   └── AuditLog.vue
│   │   └── admin/
│   │       └── Settings.vue
│   ├── router/
│   │   └── index.ts
│   ├── locale/
│   │   ├── zh-CN.json
│   │   └── en-US.json
│   ├── styles/
│   │   ├── element-overrides.scss
│   │   └── variables.scss
│   └── utils/
│       ├── format.ts                    # 字节、时间格式化
│       ├── filename-escape.ts           # XSS 防御
│       └── error.ts
└── tests/
    ├── unit/
    └── e2e/
```

---

## 2. 路由与导航

### 2.1 路由表

```typescript
const routes = [
  { path: '/login', component: Login },
  { path: '/auth/callback', component: AuthCallback },
  {
    path: '/',
    component: AppLayout,
    meta: { requiresAuth: true },
    children: [
      { path: '', component: Dashboard },
      { path: 'tasks', component: TaskList },
      { path: 'tasks/new', component: TaskCreate },
      { path: 'tasks/:id', component: TaskDetail, props: true },
      { path: 'executors', component: ExecutorList, meta: { roles: ['operator+'] } },
      { path: 'executors/:id', component: ExecutorDetail, meta: { roles: ['operator+'] } },
      { path: 'search', component: ModelSearch },
      { path: 'quota', component: Quota, meta: { roles: ['admin'] } },
      { path: 'audit', component: AuditLog, meta: { roles: ['audit_reader'] } },
      { path: 'settings', component: Settings, meta: { roles: ['admin'] } },
    ],
  },
];
```

### 2.2 主导航

```
┌──────────────────────────────────────────────────────────────────┐
│  [LOGO] DLW                                  Tenant: team-a  ▾  │
│                                              user@team.com  [⏻] │
├──────┬───────────────────────────────────────────────────────────┤
│      │                                                           │
│  📊  │  Dashboard / 任务总览                                      │
│  ☰   │                                                           │
│      │                                                           │
│  📋 任│                                                           │
│      │                                                           │
│  🖥️ 节│                                                           │
│      │                                                           │
│  🔍 搜│                                                           │
│      │                                                           │
│  📊 配│                                                           │
│      │                                                           │
│  📜 审│ (admin only)                                              │
│      │                                                           │
│  ⚙️  │                                                           │
└──────┴───────────────────────────────────────────────────────────┘
```

折叠态：左侧仅图标，宽度 60px。展开态宽度 220px。

---

## 3. 核心页面 Wireframe

> 9 个常规页面 + 1 个 AI Copilot 浮动面板（v2.1）。详见 [12-ai-copilot.md §8](./12-ai-copilot.md)。

### 3.1 Dashboard / 总览

```
┌──────────────────────────────────────────────────────────────────┐
│ Dashboard                                       2026-04-28 14:30 │
├──────────────────────────────────────────────────────────────────┤
│ ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐           │
│ │ 进行中   │  │ 今日完成 │  │ 失败     │  │ 节点健康 │            │
│ │   12     │  │   38     │  │   2      │  │  9 / 10  │           │
│ │ ▲▲ 3     │  │ ▲▲ 5     │  │  ↓ 1     │  │  💚 ⚠️    │            │
│ └──────────┘  └──────────┘  └──────────┘  └──────────┘           │
│                                                                   │
│ ┌─────────────────────────────────────────────────────────────┐  │
│ │ 集群吞吐 (近 24h)                                            │  │
│ │ 4 GB/s ┤                          ╱╲                        │  │
│ │ 3 GB/s ┤                  ╱──╲   ╱  ╲                       │  │
│ │ 2 GB/s ┤        ╱╲       ╱    ╲_╱    ╲       ╱╲             │  │
│ │ 1 GB/s ┤  ____ ╱  ╲_____╱              ╲_____╱  ╲___        │  │
│ │        └─────────────────────────────────────────────►      │  │
│ │        00:00      06:00      12:00      18:00     24:00     │  │
│ └─────────────────────────────────────────────────────────────┘  │
│                                                                   │
│ ┌──────────────────────────────┐ ┌─────────────────────────────┐ │
│ │ 最近任务                     │ │ 配额（本月）                │ │
│ │  ✅ Qwen3-72B    14 min ago  │ │ 流量  ▓▓▓▓▓▓▓▓░░  78%      │ │
│ │  🔄 DeepSeek-V3  下载中 67%  │ │ 存储  ▓▓▓▓▓▓░░░░  62%      │ │
│ │  🔄 GLM-4-9B     校验中 92%  │ │ 任务  3 / 10 并发          │ │
│ │  ❌ Llama-3.1    校验失败    │ │                            │ │
│ │  ✅ Mistral-7B   1 hour ago  │ │ [详情]                     │ │
│ │  [查看更多]                  │ └────────────────────────────┘ │
│ └──────────────────────────────┘                                 │
│                                                                   │
│ ┌─────────────────────────────────────────────────────────────┐  │
│ │ 系统告警 (无)                                                │  │
│ │ 全部 SLO 健康，无 P0/P1 告警                                  │  │
│ └─────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────┘
```

**组件**：`<KpiCard>` × 4、`<SpeedChart>` (ECharts)、`<RecentTaskList>`、`<QuotaSummary>`、`<AlertBanner>`

### 3.2 任务列表

```
┌──────────────────────────────────────────────────────────────────┐
│ 任务  > 列表                                       [+ 创建任务]  │
├──────────────────────────────────────────────────────────────────┤
│ 状态: [全部 ▾]  优先级: [全部 ▾]  Project: [全部 ▾]   [🔍 搜索] │
├──────────────────────────────────────────────────────────────────┤
│ ☐  状态     模型                  进度          创建时间   操作 │
├──────────────────────────────────────────────────────────────────┤
│ ☐  🔄 下载  deepseek-ai/DeepSeek-V3  ▓▓▓▓░░░░ 67%  10 min  [详] │
│            689 GB / 163 文件  ETA 18 min          ago     [取] │
│            源: ModelScope ✓  HF Mirror ✓                         │
│ ─────────────────────────────────────────────────────────────── │
│ ☐  🔍 校验  Qwen/Qwen3-72B-Instruct ▓▓▓▓▓▓▓▓ 92%  25 min  [详] │
│            144 GB / 30 文件                       ago           │
│ ─────────────────────────────────────────────────────────────── │
│ ☐  ✅ 完成  THUDM/GLM-4-9B          ▓▓▓▓▓▓▓▓100%  1h 12m  [详] │
│            18.5 GB / 10 文件  耗时 3m 22s         ago           │
│ ─────────────────────────────────────────────────────────────── │
│ ☐  ❌ 失败  meta-llama/Llama-3.1-8B               2h 30m  [详] │
│            ❗ License 未审批                       ago    [重试]│
│ ─────────────────────────────────────────────────────────────── │
│ ☐  ⏸️ 暂停  mistralai/Mistral-7B    ▓▓▓░░░░░ 34%  5h 12m  [详] │
│            HF 全局限流，将于 5 min 后重试         ago     [取] │
├──────────────────────────────────────────────────────────────────┤
│ [批量取消] [批量重试]              共 47 条    < 1 2 3 ... 8 > │
└──────────────────────────────────────────────────────────────────┘
```

**核心组件**：

```vue
<TaskList>
  <TaskFilter v-model="filter" />     <!-- 顶部过滤 -->
  <TaskRow
    v-for="task in tasks"
    :key="task.id"
    :task="task"
    @click="navigateToDetail(task.id)"
  />
  <Pagination v-model="page" :total="total" />
</TaskList>
```

`<TaskRow>` props: `task: DownloadTask`，emit: `cancel` / `retry`。
状态徽章颜色：pending=灰、scheduling=蓝、downloading=蓝(动)、verifying=橙、completed=绿、failed=红、cancelling=黄、cancelled=灰、paused_external=黄。

### 3.3 任务详情（最复杂的页面）

```
┌──────────────────────────────────────────────────────────────────┐
│ 任务 > deepseek-ai/DeepSeek-V3                  [取消] [调优先级]│
├──────────────────────────────────────────────────────────────────┤
│ 基本信息                                                          │
│ ┌─────────────────────────────────────────────────────────────┐  │
│ │ Repo:       deepseek-ai/DeepSeek-V3                          │  │
│ │ Revision:   abc123def4567890abc123def4567890abc12345          │  │
│ │ Storage:    s3://prod-bucket/team-a/...                      │  │
│ │ Owner:      alice@team.com    Project: research              │  │
│ │ 优先级:     ⭐⭐ (Normal)        Trace: c0ffee01...      [复制]│  │
│ │ 创建于:     2026-04-28 14:23 (10 min ago)                    │  │
│ │ Source 策略: 自动多源加速                                    │  │
│ └─────────────────────────────────────────────────────────────┘  │
│                                                                   │
│ ┌──────────────────────────────────┐ ┌─────────────────────────┐ │
│ │ 进度                             │ │ 速度                    │ │
│ │                                  │ │                         │ │
│ │      ╭───────╮                   │ │  当前  1.2 GB/s          │ │
│ │     │  67%  │                   │ │  平均  980 MB/s          │ │
│ │      ╰───────╯                   │ │  ETA   ~18 min           │ │
│ │  462 / 689 GB                    │ │                         │ │
│ │  108 / 163 文件                  │ │  线程  5 节点 × 8 = 40   │ │
│ │                                  │ │                         │ │
│ └──────────────────────────────────┘ └─────────────────────────┘ │
│                                                                   │
│ 源分配 (file-level + chunk-level) ────────────────────── [详情] │
│ ┌─────────────────────────────────────────────────────────────┐  │
│ │ ModelScope    ████████████████████████  62%  428 GB  950MB/s│  │
│ │ HF Mirror     ████████████              28%  193 GB  420MB/s│  │
│ │ HuggingFace   ████                      10%   68 GB   85MB/s│  │
│ │                                                              │  │
│ │ 大文件 chunk 路由 (model-00001-of-00163.safetensors, 4.3GB)  │  │
│ │   chunk 0/8  537 MB  ModelScope    ✅ 完成                   │  │
│ │   chunk 1/8  537 MB  HF Mirror     ✅ 完成                   │  │
│ │   chunk 2/8  537 MB  ModelScope    🔄 412/537 MB (76%)       │  │
│ │   chunk 3/8  537 MB  ModelScope    ⏸ pending                 │  │
│ │   ...                                                         │  │
│ └─────────────────────────────────────────────────────────────┘  │
│                                                                   │
│ 文件矩阵 (163 文件)  ─────────  状态: ✅ 已完成  🔄 下载中  ⏸ 等待│
│ ┌─────────────────────────────────────────────────────────────┐  │
│ │ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ │  │
│ │ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ │  │
│ │ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ │  │
│ │ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ │  │
│ │ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ │  │
│ │ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ 🔄 🔄 🔄 🔄 🔄 ⏸ ⏸ ⏸ ⏸ ⏸ ⏸ ⏸  │  │
│ │ ⏸ ⏸ ⏸                                                       │  │
│ │ 共 163  ✅ 108  🔄 5  ⏸ 50         hover 查看文件名          │  │
│ └─────────────────────────────────────────────────────────────┘  │
│                                                                   │
│ 节点参与                                                          │
│ ┌─────────────────────────────────────────────────────────────┐  │
│ │ host-01-w1  💚  下载中  3 文件  当前 950 MB/s    ModelScope │  │
│ │ host-01-w2  💚  下载中  2 文件  当前 920 MB/s    ModelScope │  │
│ │ host-02-w1  💚  上传中  1 文件                              │  │
│ │ host-03-w1  💚  下载中  2 文件  当前 420 MB/s    HF Mirror   │  │
│ │ host-04-w1  ⚠️  degraded  闲置                               │  │
│ └─────────────────────────────────────────────────────────────┘  │
│                                                                   │
│ 事件日志 ─────────────────────────────────── [全部 / 错误 / 警告]│
│ ┌─────────────────────────────────────────────────────────────┐  │
│ │ 14:32:15 INFO  状态变更 scheduling → downloading             │  │
│ │ 14:32:18 INFO  开始测速 (5 sources × 4 executors)            │  │
│ │ 14:32:26 INFO  最优组合: ModelScope, HF Mirror              │  │
│ │ 14:32:27 INFO  分配 162 文件                                 │  │
│ │ 14:35:42 WARN  source=HuggingFace 429，全局降速 50%          │  │
│ │ 14:38:12 INFO  完成 chunk 5/8 of model-00001                 │  │
│ │ ...                                                          │  │
│ └─────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────┘
```

**性能要点**：

- 文件矩阵 163 个 cell 用 `<canvas>` 绘制，避免 163 个 DOM 节点
- WS 推送 patch 仅更新变化的 cell，每秒最多 60 帧（requestAnimationFrame）
- 事件日志虚拟滚动（vue-virtual-scroller），≤ 100 条 visible

### 3.4 任务创建

```
┌──────────────────────────────────────────────────────────────────┐
│ 任务 > 创建                                                       │
├──────────────────────────────────────────────────────────────────┤
│ 模型 *                                                            │
│ ┌─────────────────────────────────────────────────────────────┐  │
│ │ Qwen/Qwen3-72B-Instruct                              [搜索] │  │
│ │   📦 144 GB / 30 文件 · License: apache-2.0                  │  │
│ │   覆盖: HF ✓  HF Mirror ✓  ModelScope ✓                     │  │
│ └─────────────────────────────────────────────────────────────┘  │
│                                                                   │
│ Revision *                                                        │
│ ┌─────────────────────────────────────────────────────────────┐  │
│ │ main → abc123def4567890... (自动解析为 sha)                  │  │
│ │ ⚠️ 'main' 会被解析为当前 sha 后锁定                           │  │
│ └─────────────────────────────────────────────────────────────┘  │
│                                                                   │
│ Storage Backend *                                                 │
│ ┌─────────────────────────────────────────────────────────────┐  │
│ │ [team-a-prod-s3 ▾]   region: cn-north-1   余额: 3.2 TB       │  │
│ └─────────────────────────────────────────────────────────────┘  │
│                                                                   │
│ 源策略                                                            │
│   ○ 仅 HuggingFace                                                │
│   ● 自动多源加速  ← 默认                                         │
│   ○ 自定义： ☑ HF  ☑ Mirror  ☑ ModelScope  ☐ WiseModel          │
│   ○ 仅自托管 mirror（内网模式）                                  │
│   [x] 启动前实时测速（推荐，约 5-15 秒）                          │
│                                                                   │
│ 优先级           ○ Low  ● Normal  ○ High  ○ Urgent              │
│ 文件过滤         ● 仅核心 (.safetensors + config + tokenizer)    │
│                  ○ 全部                                           │
│                  ○ 自定义 glob: ___________                       │
│ 模拟模式 [ ]     （不实际下载，仅生成调度计划，用于测试）        │
│                                                                   │
│ 高级 ▾                                                            │
│   增量基线: [ 选择已完成的 revision ▾ ]                          │
│   流量上限: ___ GB                                                │
│   信任非 HF sha256 [ ] (谨慎，需 admin 审批)                      │
│                                                                   │
│ 估算                                                              │
│   预计下载 144 GB                                                 │
│   预计耗时 18 min（多源） / 1.5h（仅 HF）                         │
│   预计费用 ¥0.42（同 region 跨可用区）                            │
│                                                                   │
│                              [取消]  [创建任务]                  │
└──────────────────────────────────────────────────────────────────┘
```

**校验**：

- repo_id 实时校验（`/api/models/{id}/info?revision=main` 检测存在性）
- revision: 'main' 提交时自动解析为 sha 后再发送
- License 命中 deny 时禁用提交按钮

### 3.5 节点列表（按 host 聚合）

```
┌──────────────────────────────────────────────────────────────────┐
│ 节点                                              [+ 注册节点]   │
├──────────────────────────────────────────────────────────────────┤
│ 状态: [全部 ▾]   Region: [全部 ▾]                  共 10 节点    │
├──────────────────────────────────────────────────────────────────┤
│ ⌃ host-01.local            10 Gbps NIC   2 executors             │
│   ├─ host-01-w1  💚 healthy  下载中 3 文件  Score: 100           │
│   └─ host-01-w2  💚 healthy  下载中 2 文件  Score: 100           │
│      NIC 利用率: ▓▓▓▓▓▓▓▓░░ 78%                                   │
│      磁盘:        ▓▓▓▓░░░░░░ 38% (760GB/2TB)                     │
│                                                                   │
│ ⌃ host-02.local            10 Gbps NIC   2 executors             │
│   ├─ host-02-w1  💚 healthy  上传中 1 文件                       │
│   └─ host-02-w2  💚 healthy  闲置                                │
│                                                                   │
│ ⌃ host-04.local            10 Gbps NIC   2 executors             │
│   ├─ host-04-w1  ⚠️ degraded  连续失败 5 次                      │
│   └─ host-04-w2  💚 healthy                                     │
│      [drain] [restart]                                            │
│                                                                   │
│ ⌃ host-05.local             1 Gbps NIC   1 executor              │
│   └─ host-05-w1  ⚫ faulty   心跳超时 5 min   [details]           │
│                                                                   │
└──────────────────────────────────────────────────────────────────┘
```

### 3.6 模型搜索

```
┌──────────────────────────────────────────────────────────────────┐
│ 模型搜索                                                          │
├──────────────────────────────────────────────────────────────────┤
│ [🔍 deepseek                                              ] [搜] │
│ Pipeline: [全部 ▾]   排序: [downloads ▾]                         │
├──────────────────────────────────────────────────────────────────┤
│ deepseek-ai/DeepSeek-V3                          ⭐ 12.5k  [创建]│
│   text-generation · License: deepseek-license                   │
│   updated 3 days ago · 689 GB · 163 files                        │
│   覆盖: HF ✓  Mirror ✓  ModelScope ✓                            │
│ ─────────────────────────────────────────────────────────────── │
│ deepseek-ai/DeepSeek-Coder-V2                    ⭐ 8.2k   [创建]│
│   text-generation · License: deepseek-license                   │
│   updated 1 week ago · 470 GB                                    │
│   覆盖: HF ✓  Mirror ✓  ModelScope ✓                            │
│ ─────────────────────────────────────────────────────────────── │
│ deepseek-ai/DeepSeek-V2.5                        ⭐ 5.6k   [创建]│
│   text-generation                                                │
│ ...                                                              │
└──────────────────────────────────────────────────────────────────┘
```

### 3.7 配额与计量

```
┌──────────────────────────────────────────────────────────────────┐
│ 配额与计量                                          Tenant: team-a│
├──────────────────────────────────────────────────────────────────┤
│ 本月用量                                                          │
│ ┌─────────────────────────────┐ ┌─────────────────────────────┐  │
│ │ 流量                         │ │ 存储                         │  │
│ │  ▓▓▓▓▓▓▓▓░░ 78%             │ │  ▓▓▓▓▓▓░░░░ 62%             │  │
│ │  39 / 50 TB                  │ │  3.2 / 5.1 TB                │  │
│ │  剩余 12 天                  │ │  对象数 12,453               │  │
│ │  📈 预测月底: 47 TB          │ └─────────────────────────────┘  │
│ │  ⚠️ 接近上限                 │                                  │
│ └─────────────────────────────┘ ┌─────────────────────────────┐  │
│                                  │ 并发                         │  │
│                                  │  3 / 10 任务                 │  │
│                                  └─────────────────────────────┘  │
│                                                                   │
│ 按 Project 分组（本月）                                          │
│ ┌─────────────────────────────────────────────────────────────┐  │
│ │ research     ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓ 28 TB   45 任务  62%           │  │
│ │ inference    ▓▓▓▓▓▓▓▓ 11 TB           18 任务  28%           │  │
│ │ default      ▓▓▓▓ 5 TB                12 任务  10%           │  │
│ └─────────────────────────────────────────────────────────────┘  │
│                                                                   │
│ Top 10 模型（按字节）                                             │
│ ┌─────────────────────────────────────────────────────────────┐  │
│ │ deepseek-ai/DeepSeek-V3        12 TB    18 次               │  │
│ │ Qwen/Qwen3-72B                 9 TB     63 次               │  │
│ │ ...                                                         │  │
│ └─────────────────────────────────────────────────────────────┘  │
│                                                                   │
│ Chargeback 报表  [本月] [上月] [自定义]            [⬇ 导出 PDF] │
└──────────────────────────────────────────────────────────────────┘
```

### 3.8 审计日志

```
┌──────────────────────────────────────────────────────────────────┐
│ 审计日志                                       链完整性: ✓ 已校验│
├──────────────────────────────────────────────────────────────────┤
│ 时间范围: [近7天 ▾]   Actor: [全部 ▾]   Action: [全部 ▾] [搜索] │
├──────────────────────────────────────────────────────────────────┤
│ 2026-04-28 14:32:15  alice@team.com   192.168.1.10   ✓ success  │
│   action: task.create                                             │
│   resource: download_tasks/uuid-...                              │
│   trace: c0ffee01...                                              │
│   payload: {repo_id: "deepseek-ai/...", revision: "abc..."}      │
│ ─────────────────────────────────────────────────────────────── │
│ 2026-04-28 14:30:02  bob@team.com   10.0.0.5   ✗ denied         │
│   action: task.create                                             │
│   resource: download_tasks/-                                     │
│   reason: REPO_GATED — pending approval                          │
│ ─────────────────────────────────────────────────────────────── │
│ 2026-04-28 14:25:30  system                          ✓ success  │
│   action: executor.register                                       │
│   resource: executors/host-01-w1                                 │
│   trace: ...                                                     │
└──────────────────────────────────────────────────────────────────┘
```

### 3.9 系统设置（admin）

（AI Copilot 浮动面板见 §3.10）

```
┌──────────────────────────────────────────────────────────────────┐
│ 系统设置                                                          │
├──────────────────────────────────────────────────────────────────┤
│ ⌃ 源驱动                                                         │
│   ☑ HuggingFace        端点: https://huggingface.co               │
│   ☑ HF Mirror          端点: https://hf-mirror.com                │
│   ☑ ModelScope         端点: https://www.modelscope.cn            │
│   ☐ WiseModel          [启用]                                    │
│   ☐ OpenCSG            [启用]                                    │
│   [+ 添加自托管 mirror]                                          │
│                                                                   │
│ ⌃ License 策略                                                   │
│   apache-2.0         ● Allow                                     │
│   mit                ● Allow                                     │
│   gpl-3.0            ○ Allow ● Warn ○ Deny                       │
│   meta-llama-3       ○ Allow ● Warn ○ Deny                       │
│   [+ 添加策略]                                                   │
│                                                                   │
│ ⌃ HF Token（envelope-encrypted）                                  │
│   Primary    hf_xxx...xxx (last rotated 14d ago)  [轮换]         │
│   Secondary  -                                     [设置]        │
│                                                                   │
│ ⌃ 维护模式                                                       │
│   状态: 正常运行                                                 │
│   [进入维护模式]                                                  │
└──────────────────────────────────────────────────────────────────┘
```

### 3.10 AI Copilot 浮动面板（v2.1）

> 入口：右下角浮动 🤖 / `Ctrl+K`。详细设计见 [12-ai-copilot.md §8](./12-ai-copilot.md)。

主要交互：

```
┌─────────────────────────────────────────────────────────────┐
│ 🤖 AI Copilot                              [⚙] [─] [✕]     │
├─────────────────────────────────────────────────────────────┤
│  你: 下载 DeepSeek 最新发布的 V3 模型                       │
│                                                             │
│  🤖 我先查 deepseek-ai 最近 30 天发布的模型...              │
│  🛠 dlw_list_recent_models(deepseek-ai, days=30) ✓ 找到 3   │
│  🛠 dlw_get_model_info(DeepSeek-V3, main) ✓ 689 GB / 163    │
│                                                             │
│  ┌─ 🛠 创建下载任务 ────────────────────────────────────┐   │
│  │  Repo: deepseek-ai/DeepSeek-V3                       │   │
│  │  Revision: abc123def... (resolved from 'main')       │   │
│  │  预计流量: 689 GB (12.4% 月配额)                     │   │
│  │  AI 解释：自动多源加速，到默认 storage               │   │
│  │  [取消] [修改] [✓ 确认]                              │   │
│  └──────────────────────────────────────────────────────┘   │
├─────────────────────────────────────────────────────────────┤
│ 输入消息... (Shift+Enter 换行)                       [发送] │
│ 12,453 tokens · $0.18 · 本月剩余 62%   [清空] [/help]      │
└─────────────────────────────────────────────────────────────┘
```

关键前端组件（v2.1 引入）：

| 组件 | 用途 |
|------|------|
| `<AICopilotPanel>` | 浮动面板根 |
| `<AIMessageStream>` | SSE 流式消息渲染（含 thinking / message_delta） |
| `<AIToolCallCard>` | 工具调用卡片（read-only / pending_confirm 两态） |
| `<AIConfirmModal>` | 写操作确认弹窗（含 quota impact 估算） |
| `<AIConversationHistory>` | 历史会话列表（按 last_message_at） |
| `<AITokenUsage>` | 底部 token / 成本指示 |

状态管理：

- 新增 `useAICopilotStore`（pinia）—— 当前 conversation、待确认 tool calls、token 用量
- SSE 通过 `EventSource` 而非 WebSocket（单向流式 + 自动重连）
- 上下文感知：组件挂载时把当前 route + selected entity 注入到 `context` 字段（详见 12 §8.5）

---

## 4. 组件库

### 4.1 共享组件清单

| 组件 | 用途 | Props |
|------|------|-------|
| `<KpiCard>` | Dashboard 指标卡 | `label, value, delta?, trend?` |
| `<TaskRow>` | 任务列表行 | `task: DownloadTask` |
| `<TaskProgressRing>` | 圆环进度 | `progress: 0-100, size, label?` |
| `<FileMatrix>` | 文件状态矩阵 | `subtasks: FileSubTask[]` （canvas 渲染） |
| `<SourceAllocationView>` | 源分配可视化 | `allocation: SourceAllocation` |
| `<HealthBadge>` | 健康状态徽章 | `status: ExecutorStatus, score?` |
| `<SpeedChart>` | ECharts 速度曲线 | `series, timeRange` |
| `<QuotaBar>` | 配额进度条 | `used, limit, label` |
| `<TraceLink>` | 跳转 Grafana | `traceId` |

### 4.2 Vue SFC 例子

```vue
<!-- TaskProgressRing.vue -->
<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(defineProps<{
  progress: number      // 0-100
  size?: number         // px
  label?: string
  status?: 'normal' | 'success' | 'failed' | 'paused'
}>(), {
  size: 120,
  status: 'normal',
})

const colorMap = {
  normal: '#409EFF',
  success: '#67C23A',
  failed: '#F56C6C',
  paused: '#E6A23C',
}

const strokeColor = computed(() => colorMap[props.status])
const dashoffset = computed(() => 100 - props.progress)
</script>

<template>
  <div class="progress-ring" :style="{ width: size + 'px', height: size + 'px' }">
    <svg :width="size" :height="size" viewBox="0 0 36 36">
      <circle cx="18" cy="18" r="15.915" fill="none"
              stroke="#EBEEF5" stroke-width="3" />
      <circle cx="18" cy="18" r="15.915" fill="none"
              :stroke="strokeColor" stroke-width="3"
              stroke-dasharray="100 100"
              :stroke-dashoffset="dashoffset"
              stroke-linecap="round"
              transform="rotate(-90 18 18)" />
    </svg>
    <div class="ring-text">
      <div class="ring-pct">{{ progress }}%</div>
      <div v-if="label" class="ring-label">{{ label }}</div>
    </div>
  </div>
</template>

<style scoped>
.progress-ring {
  position: relative;
  display: inline-block;
}
.ring-text {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
}
.ring-pct { font-size: 24px; font-weight: 600; }
.ring-label { font-size: 12px; color: #909399; }
</style>
```

---

## 5. 状态管理（Pinia）

### 5.1 Store 划分

```
useAuthStore        当前用户、tenant、token
useTasksStore       任务列表分页缓存（vue-query 也用，store 只存 UI 选择）
useTaskDetailStore  当前打开的任务详情（含 WS 增量合并）
useExecutorsStore   节点列表
useUiStore          主题、菜单收起、当前语言
useToastStore       全局提示
```

### 5.2 progress store（最关键，承载 WS 增量）

```typescript
// stores/progress.ts
import { defineStore } from 'pinia'

export const useProgressStore = defineStore('progress', () => {
  // taskId -> 实时状态
  const tasks = reactive(new Map<string, TaskRuntime>())

  // 当前订阅的 task ids
  const subscribed = ref(new Set<string>())

  // WS 序列号（每 connection 一个）
  let lastSeq = 0
  let ws: WebSocket | null = null

  function subscribe(taskIds: string[]) {
    taskIds.forEach(id => subscribed.value.add(id))
    if (!ws) connect()
    else ws.send(JSON.stringify({ type: 'update_subscriptions', task_ids: [...subscribed.value] }))
  }

  function connect() {
    const url = `${WS_BASE}?token=${getJwt()}&filter=${[...subscribed.value].join(',')}`
    ws = new WebSocket(url, ['bearer.' + getJwt()])
    ws.onmessage = handleMessage
    ws.onclose = () => setTimeout(connect, 1000)
  }

  function handleMessage(evt: MessageEvent) {
    const msg = JSON.parse(evt.data)
    switch (msg.type) {
      case 'snapshot':
        for (const t of msg.tasks) tasks.set(t.id, t)
        lastSeq = msg.seq
        break

      case 'delta':
        if (msg.seq !== lastSeq + 1) {
          // gap 检测 → 主动 resync
          ws?.send(JSON.stringify({ type: 'resync', last_seq: lastSeq }))
          return
        }
        for (const patch of msg.patches) {
          mergeTaskPatch(tasks, patch)
        }
        lastSeq = msg.seq
        break

      case 'ping':
        ws?.send(JSON.stringify({ type: 'pong' }))
        break
    }
  }

  function mergeTaskPatch(tasks: Map<string, TaskRuntime>, patch: TaskPatch) {
    const existing = tasks.get(patch.task_id)
    if (!existing) return
    Object.assign(existing.fields, patch.fields)
    if (patch.subtasks) {
      for (const sp of patch.subtasks) {
        const st = existing.subtasks.find(s => s.id === sp.id)
        if (st) Object.assign(st.fields, sp.fields)
      }
    }
  }

  return { tasks, subscribe, /* ... */ }
})
```

### 5.3 数据获取（vue-query）

```typescript
// composables/useTaskList.ts
import { useQuery } from '@tanstack/vue-query'
import { client } from '@/api/client'

export function useTaskList(filter: Ref<TaskFilter>) {
  return useQuery({
    queryKey: ['tasks', filter],
    queryFn: () => client.tasks.list({ ...filter.value }),
    refetchInterval: 30 * 1000,   // 30s 兜底刷新（WS 实时）
    staleTime: 5 * 1000,
  })
}
```

### 5.4 乐观更新（取消任务）

```typescript
const queryClient = useQueryClient()
const cancelMutation = useMutation({
  mutationFn: (taskId: string) => client.tasks.cancel(taskId),
  onMutate: async (taskId) => {
    await queryClient.cancelQueries({ queryKey: ['tasks'] })
    const prev = queryClient.getQueryData<TaskListResponse>(['tasks'])
    queryClient.setQueryData(['tasks'], (old: any) => ({
      ...old,
      items: old.items.map((t: any) =>
        t.id === taskId ? { ...t, status: 'cancelling' } : t),
    }))
    return { prev }
  },
  onError: (err, taskId, ctx) => {
    // 回滚
    queryClient.setQueryData(['tasks'], ctx?.prev)
    showToast('取消失败：' + err.message, 'error')
  },
  onSettled: () => queryClient.invalidateQueries({ queryKey: ['tasks'] }),
})
```

---

## 6. WebSocket 协议封装

详见 02 §5。前端 composable：

```typescript
// composables/useWebSocket.ts
import { ref, onUnmounted } from 'vue'

interface WsOptions {
  url: string
  token: string
  onSnapshot: (data: any) => void
  onDelta: (patches: any[]) => void
  onResync: () => void
}

export function useDlwWebSocket(opts: WsOptions) {
  const status = ref<'connecting' | 'open' | 'closed' | 'error'>('connecting')
  let ws: WebSocket | null = null
  let lastSeq = 0
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null
  let reconnectDelay = 1000

  function connect() {
    status.value = 'connecting'
    const fullUrl = `${opts.url}?token=${opts.token}&last_seq=${lastSeq}`
    ws = new WebSocket(fullUrl, ['bearer.' + opts.token])

    ws.onopen = () => {
      status.value = 'open'
      reconnectDelay = 1000
    }

    ws.onmessage = (evt) => {
      const msg = JSON.parse(evt.data)
      if (msg.type === 'snapshot') {
        opts.onSnapshot(msg.tasks)
        lastSeq = msg.seq
      } else if (msg.type === 'delta') {
        if (msg.seq !== lastSeq + 1) {
          // gap → resync
          ws?.send(JSON.stringify({ type: 'resync', last_seq: lastSeq }))
          opts.onResync()
          return
        }
        opts.onDelta(msg.patches)
        lastSeq = msg.seq
      } else if (msg.type === 'ping') {
        ws?.send(JSON.stringify({ type: 'pong' }))
      }
    }

    ws.onclose = () => {
      status.value = 'closed'
      reconnectTimer = setTimeout(connect, reconnectDelay)
      reconnectDelay = Math.min(reconnectDelay * 2, 30000)  // 指数退避
    }

    ws.onerror = () => { status.value = 'error' }
  }

  function close() {
    if (reconnectTimer) clearTimeout(reconnectTimer)
    ws?.close()
  }

  connect()
  onUnmounted(close)

  return { status, close }
}
```

---

## 7. 主题与 i18n

### 7.1 主题（Element Plus tokens 覆盖）

```scss
// styles/element-overrides.scss
@forward 'element-plus/theme-chalk/src/common/var.scss' with (
  $colors: (
    'primary': (
      'base': #1890ff,
    ),
  ),
);
```

支持深色模式（cssvar 切换）。

### 7.2 i18n

```typescript
// locale/zh-CN.json
{
  "common": {
    "create": "创建",
    "cancel": "取消",
    "retry": "重试"
  },
  "task": {
    "status": {
      "pending": "等待",
      "scheduling": "调度中",
      "downloading": "下载中",
      "verifying": "校验中",
      "completed": "已完成",
      "failed": "失败",
      "cancelling": "取消中",
      "cancelled": "已取消"
    }
  }
}
```

---

## 8. 安全（前端层）

### 8.1 XSS 防御（解决 04 §4.4 的 SEC-03）

- 全局禁用 `v-html`，eslint rule:
  ```javascript
  // .eslintrc.cjs
  rules: {
    'vue/no-v-html': 'error',
  }
  ```
- 来自 executor 的字符串（filename, error_message）渲染前 escape
- CSP via `<meta http-equiv>`：`default-src 'self'; script-src 'self'`

### 8.2 CSRF

- API 请求自动加 `X-CSRF-Token` header（从 cookie 读取）
- SameSite=Strict cookie

### 8.3 Token 存储

- access_token：内存（避免 localStorage XSS 风险）
- refresh_token：HttpOnly cookie（前端不可读）

---

## 9. 性能要点

| 点 | 策略 |
|----|------|
| 任务列表 | 虚拟滚动（vue-virtual-scroller），只渲染可见 |
| 文件矩阵 163 cell | `<canvas>` 绘制，避免 DOM 节点 |
| WS 推送 | RAF 节流，每帧最多更新一次 store |
| 大表格 | 服务端分页 + cursor |
| ECharts 实时 | `notMerge: false` 增量更新 |
| 路由 | code splitting per route |
| 包大小 | Element Plus 按需引入；Vite 自动 tree-shaking |

---

## 10. 测试

### 10.1 Unit（Vitest）

```typescript
// tests/unit/components/TaskRow.spec.ts
import { mount } from '@vue/test-utils'
import TaskRow from '@/components/task/TaskRow.vue'

test('renders downloading status with progress bar', () => {
  const wrapper = mount(TaskRow, {
    props: {
      task: { id: '1', status: 'downloading', progress: { ... } },
    },
  })
  expect(wrapper.text()).toContain('下载中')
  expect(wrapper.find('.progress-bar')).toBeTruthy()
})
```

### 10.2 E2E（Playwright）

```typescript
// tests/e2e/task-create.spec.ts
test('user can create and watch a download task', async ({ page }) => {
  await loginAs(page, 'alice@team.com')
  await page.goto('/tasks/new')
  await page.fill('[data-test=repo-input]', 'Qwen/Qwen3-7B')
  await page.click('[data-test=submit]')
  await expect(page.locator('[data-test=task-status]')).toContainText('调度中')

  // 等任务进入 downloading
  await page.waitForSelector('[data-test=task-status]:has-text("下载中")', { timeout: 30000 })

  // 文件矩阵渲染
  await expect(page.locator('canvas[data-test=file-matrix]')).toBeVisible()
})
```

---

## 11. 与其他文档的链接

- API 协议：→ [02-protocol.md](./02-protocol.md)
- WS 协议：→ [02-protocol.md](./02-protocol.md) §5
- 安全要求：→ [04-security-and-tenancy.md](./04-security-and-tenancy.md) §4
- CLI/SDK 对比：→ [11-cli-and-sdk-spec.md](./11-cli-and-sdk-spec.md)
- 测试：→ [07-test-plan.md](./07-test-plan.md)
- Phase 计划：→ [08-mvp-roadmap.md](./08-mvp-roadmap.md)
