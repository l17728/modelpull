# Demo Screenshots

1280x800 viewport, captured via Playwright MCP against the local dev stack
(controller `:8000` + Vite dev server). Re-shoot:

```bash
# Terminal 1 — controller
DLW_BEARER_TOKEN=demo-smoke-token uv run uvicorn dlw.main:app --port 8000

# Terminal 2 — frontend
cd frontend && pnpm dev

# Terminal 3 — seed tasks via curl, then drive Playwright through
# /login → /paste-token → / → click row → /tasks/<uuid>
```

| File | Page | What it shows |
|------|------|---------------|
| `01-login.png` | `/login` | Element Plus form, single password-type input, zh-CN labels |
| `02-task-list.png` | `/` | ElTable with 6 tasks: 5 排队中 + 1 成功; status badges + zh-CN dates |
| `03-task-detail-active.png` | `/tasks/<pending-uuid>` | Summary card + subtasks + "实时刷新中…" indicator (1s polling active) |
| `04-task-detail-completed.png` | `/tasks/<succeeded-uuid>` | Same layout; "已停止刷新（终态）" indicator (polling auto-stopped) |

Used in `docs/demo/pitch.md` (Frame 2) and `docs/demo/runbook.md` (UI section).
