# SP4e follow-on — Structured sanitize choke point (inv-19 enforcement)

## Problem

SP4e (PR #34) added `sanitize_external` / `sanitize_t2` (`src/dlw/ai/sanitize.py`) and wired sanitization **per-tool inline**: `hf_api_metadata` and `hf_model_card` pre-wrap their whole output; `dlw_get_task` and `dlw_get_task_events` call `sanitize_external` inline on their `error_message` / `message` fields. The SP4e spec explicitly deferred a **non-bypassable structured choke point** as a named follow-on: "the structural guarantee against future omission is the follow-on."

Two real gaps that come from this state:
1. **`dlw_list_tasks` was overlooked**: it returns `{"items": [TaskRead.model_dump(), ...]}`, and each item carries `error_message` — but this tool does NOT call sanitize inline. A pre-existing latent inv-19 gap.
2. **Future tools will silently drift**: any new tool that returns external bytes and forgets to call `sanitize_external` will silently feed unsanitized content into the LLM context.

This sub-project closes both gaps with a declarative choke point.

## §0 Design — `Tool.external_fields` + choke point in `call_tool`

### Add a declarative field to `Tool`

`src/dlw/ai/tools.py::Tool` (and the `write_tools.py` mirror — same dataclass shape) gain one optional attribute:

```python
@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    run: Callable[..., Awaitable[dict]]
    # SP4e follow-on: declarative external-content field paths. The choke point
    # in service.call_tool applies sanitize_external() to each path before the
    # tool result is attached to a tool_result event. Path syntax:
    #   "field"           — top-level string field
    #   "field[].nested"  — iterate items in `field` (list of dicts), sanitize
    #                        nested string on each
    # Semantics:
    #   None  → tool fully pre-wraps output (e.g., hf_model_card.sanitized);
    #           choke point applies NO sanitization (avoids double-wrap).
    #   []    → tool returns only internal data; no fields to sanitize.
    #   [...] → choke point sanitizes those paths IF the value is not already
    #           wrapped (idempotency guard against accidental double-wrap).
    external_fields: list[str] | None = field(default_factory=list)
```

`default_factory=list` (not `None`) is the **safe default**: every existing tool that doesn't set this gets `[]`, meaning "no external fields, no sanitization needed." Tools that pre-wrap whole outputs must opt out by setting `external_fields=None`.

### Per-tool declarations

| Tool                       | `external_fields`                           | Replaces inline sanitize? |
|---                          |---                                          |---                        |
| `dlw_list_tasks`           | `["items[].error_message"]`                 | **new** (latent gap fix)  |
| `dlw_get_task`             | `["error_message"]`                         | yes (remove inline call)  |
| `dlw_get_task_events`      | `["items[].message"]`                       | yes (remove inline call)  |
| `dlw_quota_current`        | `[]` (default)                              | n/a                       |
| `hf_api_metadata`          | `None` (pre-wraps `files_sanitized`)        | n/a (pre-wraps)           |
| `hf_model_card`            | `None` (pre-wraps `sanitized`)              | n/a (pre-wraps)           |
| `dlw_cancel_task`          | `[]` (default — internal only)              | n/a                       |
| `dlw_create_task`          | `[]` (default — internal only)              | n/a                       |

The inline calls in `tools.py::_get_task` and `_get_task_events` are **removed**. Output is byte-identical (same `sanitize_external(..., source=...)` applied to the same field). The choke point becomes the single source of truth.

### Choke point in `service.call_tool`

`src/dlw/ai/service.py::call_tool` runs the tool, catches errors, writes audit, returns a dict. The choke point inserts **between `result = await tool.run(...)` and `return result`**:

```python
result = await tool.run(...)
if tool.external_fields:
    from dlw.ai._sanitize_apply import apply_external_fields
    apply_external_fields(result, tool.external_fields, source=f"tool:{tool.name}")
return result
```

`tool.external_fields is None` → no-op (pre-wrap path).
`tool.external_fields == []` → falsy → no-op.
`tool.external_fields == [...]` → applied.

### `_sanitize_apply.py` helper

New file `src/dlw/ai/_sanitize_apply.py`:

```python
"""SP4e follow-on: choke-point helper that applies sanitize_external to
declared field paths in a tool result. Idempotent: skips already-wrapped
values (those starting with the boundary tag), so accidentally declaring a
pre-wrapped field is harmless."""
from __future__ import annotations

from dlw.ai.sanitize import sanitize_external

_ALREADY_WRAPPED_PREFIXES = ("<external_content", "<external_user_content")


def _is_wrapped(s: str) -> bool:
    return any(s.startswith(p) for p in _ALREADY_WRAPPED_PREFIXES)


def _sanitize_leaf(node: dict, key: str, *, source: str) -> None:
    val = node.get(key)
    if not isinstance(val, str) or not val or _is_wrapped(val):
        return
    node[key] = sanitize_external(val, source=source).text


def _apply_one(node, parts: list[str], *, source: str) -> None:
    if not parts or not isinstance(node, dict):
        return
    head = parts[0]
    rest = parts[1:]
    if head.endswith("[]"):
        key = head[:-2]
        items = node.get(key)
        if not isinstance(items, list):
            return
        for item in items:
            _apply_one(item, rest, source=source)
    elif rest:
        nxt = node.get(head)
        if isinstance(nxt, dict):
            _apply_one(nxt, rest, source=source)
    else:
        _sanitize_leaf(node, head, source=source)


def apply_external_fields(
    result: dict, paths: list[str], *, source: str,
) -> None:
    """In-place. Walks each dotted path (with `[]` list-iteration suffix) and
    applies sanitize_external to the leaf string if not already boundary-wrapped."""
    if not isinstance(result, dict):
        return
    for p in paths:
        _apply_one(result, p.split("."), source=source)
```

The helper:
- **Idempotent**: already-wrapped values pass through untouched (defense against double-wrap if a tool both pre-wraps a field AND declares it in `external_fields`).
- **Permissive on shape**: missing keys, wrong types, empty lists — all are silent no-ops. The choke point never raises.
- **In-place**: mutates `result`; no copy. Matches the convention of the existing inline calls in `tools.py`.
- **`[]` suffix only at one level**: `external_fields=["items[].message"]` is enough for the current tools. Nested `items[].sub[].x` would also work mechanically but no tool needs it; documented as a path-syntax constraint.

### Why this exact design

- **Declarative, not magic**: tool authors must declare `external_fields=[...]`. Forgetting it on a new tool that returns external bytes leaves a gap — but the gap is now in **one obvious location per tool definition**, easy to audit. Compare with the inline status quo where the gap could be in any function body.
- **Backwards compatible**: existing pre-wrap tools (`hf_*`) set `external_fields=None` and behave exactly as before. Internal tools default to `[]` and skip the choke point.
- **One source of truth**: removes the inline `sanitize_external` calls in `tools.py::_get_task` and `_get_task_events`. Future audits look at `external_fields` declarations, not at tool bodies.
- **No double-wrap**: the `_is_wrapped` idempotency check means a tool can both pre-wrap AND declare a field without harm.

## §1 Threat model

- **What's protected**: every tool result that returns external bytes from a declared field path is wrapped at the choke point before reaching the LLM. The previous SP4e per-tool guarantee is preserved (because the choke point applies the same `sanitize_external`); the `dlw_list_tasks` gap is closed.
- **What's NOT protected**: a new tool author who forgets to declare `external_fields` AND has external bytes in an undeclared field. The choke point is a defense-in-depth layer, not a panacea. The mitigation is code review on the `external_fields` declaration (single line per tool).
- **No double-wrap**: idempotency check guarantees `<external_content><external_content>...` never appears.
- **No new audit semantics**: the choke point does not write audit (the tool already audits via `write_audit` in `service.call_tool`). The choke point is a pure metadata-mutation step.

## §2 Tests

`tests/ai/test_sanitize_apply.py` (new):
- `apply_external_fields` mutates leaf field in place; wraps content with `<external_content source="tool:X">`.
- `[]` syntax: iterates list of dicts, sanitizes nested string on each.
- Missing key / wrong type / empty list → silent no-op (no exception).
- Already-wrapped value (starts with `<external_content`) → unchanged.
- Already-wrapped T2 value (starts with `<external_user_content`) → unchanged.
- `None` paths arg → not callable (TypeError) — the choke point checks `if tool.external_fields:` so `None` is never passed; helper just documents the invariant.

`tests/ai/test_tools.py` (extend):
- New regression: `dlw_list_tasks` items with `error_message` are now sanitized (the latent gap fix).
- Existing: `dlw_get_task.error_message` still arrives wrapped (now via choke point, not inline).
- Existing: `dlw_get_task_events.items[].message` still arrives wrapped (via choke point).
- Existing: `hf_api_metadata.files_sanitized` still wrapped exactly once (no double-wrap from choke point applying to a None-marked tool).
- Existing: `hf_model_card.sanitized` still wrapped exactly once.

`tests/ai/test_external_tools.py`: unchanged (the inline-test of HF tools still asserts pre-wrap shape; choke point applies `None`-pathed no-op to them).

`tests/api/test_ai_chat.py`: SSE end-to-end test unchanged; verifies `tool_result.output` carries sanitized content (now via choke point).

## §3 Files

- **Modify** `src/dlw/ai/tools.py` — add `external_fields: list[str] | None = field(default_factory=list)` to `Tool` dataclass; declare per-tool `external_fields`; **remove inline `sanitize_external` calls** from `_get_task` and `_get_task_events`.
- **Modify** `src/dlw/ai/write_tools.py` — same `Tool` dataclass shape if it's a separate definition (else `tools.py` is the single source); declare `external_fields=[]` on write tools (or rely on default).
- **Create** `src/dlw/ai/_sanitize_apply.py` — the `apply_external_fields` helper.
- **Modify** `src/dlw/ai/service.py::call_tool` — add the choke point invocation after `await tool.run(...)`.
- **Create** `tests/ai/test_sanitize_apply.py` — helper unit tests.
- **Extend** `tests/ai/test_tools.py` — add `dlw_list_tasks` sanitize regression + verify others still wrap via choke point.

Zero migration / openapi / frontend / executor change.

## §4 Notes

- Lint gate: `pytest -q` + `lint_invariants --strict` (no new invariants; this is enforcement of existing inv-19).
- The named follow-on in the SP4e spec (lines 79–92 of `2026-05-22-ui-sp4e-external-content-sanitization-design.md`) is satisfied by this work. Inv-19 partial-compliance note can be lifted from the SP4e tracking.
- Path syntax (`field`, `field[].nested`) is intentionally minimal. Tools needing 3+ levels of list nesting can either flatten their output shape or extend the helper. The current 4 fields across 3 tools cover all real cases.
