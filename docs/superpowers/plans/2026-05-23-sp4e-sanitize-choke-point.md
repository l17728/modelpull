# SP4e follow-on — Sanitize Choke Point Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: subagent-driven-development / executing-plans. Checkbox steps.

**Goal:** Declarative `Tool.external_fields` + structural choke point in `call_tool` that auto-sanitizes external content fields, closing the `dlw_list_tasks` inv-19 gap and preventing future tool authors from forgetting to sanitize.

**Spec:** `docs/superpowers/specs/2026-05-23-sp4e-sanitize-choke-point-design.md`

**Locked constraints:**
- `Tool.external_fields` default = `field(default_factory=list)` (NOT `None`). `None` is reserved for "tool fully pre-wraps" (hf_* tools must explicitly set `external_fields=None`).
- Idempotency: choke point skips values starting with `<external_content` or `<external_user_content` (defense against double-wrap).
- Choke point fires AFTER `await tool.run(...)` and BEFORE return — in `service.py::call_tool`.
- Remove inline `sanitize_external` calls in `_get_task` / `_get_task_events` (single source of truth via choke point).
- `dlw_list_tasks` now sanitizes `items[].error_message` — this is the latent inv-19 gap fix.
- Path syntax: `"field"` for top-level string, `"field[].nested"` for list-of-dicts. The `[]` suffix is one-level only; deeper nesting not needed by any current tool.
- Helper is permissive on shape: missing keys, wrong types, empty lists → silent no-op (never raises).
- No new migration / openapi / frontend / executor change.
- Lint gates: `uv run pytest -q` + `uv run python tools/lint_invariants.py --strict`.

---

## File Structure

- **Create** `src/dlw/ai/_sanitize_apply.py` — `apply_external_fields(result, paths, *, source)` helper.
- **Modify** `src/dlw/ai/tools.py` — add `external_fields` to `Tool` dataclass; declare per-tool; remove inline sanitize calls from `_get_task` + `_get_task_events`.
- **Modify** `src/dlw/ai/write_tools.py` — if it has its own `Tool` dataclass, mirror; otherwise just confirm write tools default to `[]`.
- **Modify** `src/dlw/ai/service.py::call_tool` — wire choke point.
- **Create** `tests/ai/test_sanitize_apply.py` — helper unit tests.
- **Extend** `tests/ai/test_tools.py` — `dlw_list_tasks` sanitize regression + verify others still wrap via choke point.

---

## Milestone M1 — `_sanitize_apply.py` helper + unit tests

### Task 1: helper module + tests

**Files:** new `src/dlw/ai/_sanitize_apply.py`, new `tests/ai/test_sanitize_apply.py`.

- [ ] **Step 1 (failing tests):** read `tests/ai/test_sanitize.py` first to confirm the testing conventions for the `ai` module (sync vs async, fixture style). The helper is sync (no DB, no I/O), so the tests can be plain sync `def test_*`.

  Create `tests/ai/test_sanitize_apply.py`:

  ```python
  """SP4e follow-on: tests for apply_external_fields choke-point helper."""
  from __future__ import annotations

  from dlw.ai._sanitize_apply import apply_external_fields


  def test_top_level_leaf_wraps_in_place():
      result = {"error_message": "boom"}
      apply_external_fields(result, ["error_message"], source="tool:x")
      assert result["error_message"].startswith("<external_content")
      assert "boom" in result["error_message"]


  def test_list_iteration_wraps_each_item_field():
      result = {"items": [
          {"id": 1, "message": "ev1"},
          {"id": 2, "message": "ev2"},
      ]}
      apply_external_fields(result, ["items[].message"], source="tool:events")
      assert result["items"][0]["message"].startswith("<external_content")
      assert "ev1" in result["items"][0]["message"]
      assert result["items"][1]["message"].startswith("<external_content")
      assert "ev2" in result["items"][1]["message"]
      # Untouched fields stay as-is.
      assert result["items"][0]["id"] == 1


  def test_already_wrapped_t1_skipped():
      # If the value already starts with <external_content, the helper does
      # NOT re-wrap (idempotency / defense against double-wrap).
      pre = "<external_content source=\"x\">already wrapped</external_content>"
      result = {"error_message": pre}
      apply_external_fields(result, ["error_message"], source="tool:x")
      assert result["error_message"] == pre


  def test_already_wrapped_t2_skipped():
      pre = ("<external_user_content trust_level=\"t2\" source=\"x\">"
             "already</external_user_content>")
      result = {"sanitized": pre}
      apply_external_fields(result, ["sanitized"], source="tool:x")
      assert result["sanitized"] == pre


  def test_missing_key_is_no_op():
      result = {"other": "value"}
      apply_external_fields(result, ["error_message"], source="tool:x")
      assert result == {"other": "value"}


  def test_wrong_type_is_no_op():
      result = {"error_message": 42}  # non-string
      apply_external_fields(result, ["error_message"], source="tool:x")
      assert result["error_message"] == 42  # unchanged


  def test_empty_string_is_no_op():
      result = {"error_message": ""}
      apply_external_fields(result, ["error_message"], source="tool:x")
      assert result["error_message"] == ""


  def test_list_path_missing_or_not_list_no_op():
      apply_external_fields({"items": None}, ["items[].message"], source="t")
      apply_external_fields({}, ["items[].message"], source="t")
      # No exceptions raised → pass.


  def test_list_path_with_non_dict_items_no_op():
      result = {"items": ["string1", "string2"]}
      apply_external_fields(result, ["items[].message"], source="t")
      assert result["items"] == ["string1", "string2"]


  def test_non_dict_result_no_op():
      # The helper checks isinstance(result, dict); a list as result returns.
      apply_external_fields([], ["x"], source="t")  # type: ignore[arg-type]
      apply_external_fields(None, ["x"], source="t")  # type: ignore[arg-type]


  def test_empty_paths_no_op():
      result = {"error_message": "x"}
      apply_external_fields(result, [], source="t")
      assert result == {"error_message": "x"}


  def test_multiple_paths_applied():
      result = {
          "error_message": "err1",
          "items": [{"message": "m1"}],
      }
      apply_external_fields(
          result, ["error_message", "items[].message"], source="t")
      assert result["error_message"].startswith("<external_content")
      assert result["items"][0]["message"].startswith("<external_content")


  def test_source_propagated_into_boundary_attr():
      result = {"error_message": "boom"}
      apply_external_fields(result, ["error_message"], source="tool:dlw_get_task")
      assert "source=\"tool:dlw_get_task\"" in result["error_message"]
  ```

- [ ] **Step 2: verify FAIL** — `cd "D:/download_weights" && uv run pytest tests/ai/test_sanitize_apply.py -v` — `ImportError`.

- [ ] **Step 3 (implement helper):** create `src/dlw/ai/_sanitize_apply.py` with the exact code from spec §0 (`_sanitize_apply.py` block). Copy verbatim:

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

  **Important:** the `[]`-only-with-remainder branch: when path is `"items[].message"`, `parts = ["items[]", "message"]`, so `head="items[]"`, `rest=["message"]`. The list iteration then recurses with `rest = ["message"]` on each item. When the user writes `"items[]"` with no remainder (no field name after the bracket), the recursion bottoms out on `_apply_one(item, [], source=...)` which returns immediately — silent no-op. That's acceptable (just a wonky declaration), no need to special-case.

- [ ] **Step 4: verify PASS** — `uv run pytest tests/ai/test_sanitize_apply.py -v` — all pass.

- [ ] **Step 5: tidy + commit:**
  ```bash
  cd "D:/download_weights"
  uv run ruff check --select I001 --fix src/dlw/ai/_sanitize_apply.py tests/ai/test_sanitize_apply.py
  git add src/dlw/ai/_sanitize_apply.py tests/ai/test_sanitize_apply.py
  git commit -m "feat(sp4e): apply_external_fields choke-point helper + tests"
  ```

### Task 2: M1 gate
- [ ] `cd "D:/download_weights" && uv run pytest tests/ai/ -q` — all pass (helper is additive; no other tests touched). No commit.

---

## Milestone M2 — Wire `Tool.external_fields` + choke point + per-tool declarations

### Task 3: Tool dataclass field + per-tool declarations + service.py wiring

**Files:** `src/dlw/ai/tools.py`, `src/dlw/ai/write_tools.py`, `src/dlw/ai/service.py`, `tests/ai/test_tools.py`.

- [ ] **Step 1 (read existing code):** read these files top-to-bottom to confirm the exact shape:
  - `src/dlw/ai/tools.py` — `Tool` dataclass, `READONLY_TOOLS` dict, `_list_tasks` / `_get_task` / `_get_task_events` (find the inline `sanitize_external` calls to remove).
  - `src/dlw/ai/write_tools.py` — does it define its own `Tool` class or import from `tools.py`? If it imports, no change needed for the dataclass; only `external_fields=[]` (default) applies.
  - `src/dlw/ai/service.py::call_tool` — locate the `await tool.run(...)` line and the return point.
  - `tests/ai/test_tools.py` — understand the existing test patterns for `_list_tasks` and `_get_task`.

- [ ] **Step 2 (failing test addition):** in `tests/ai/test_tools.py`, add a regression test for the `dlw_list_tasks` sanitize gap. Pattern: mirror existing `_get_task` tests in the file. Seed a task with `error_message="boom​"` (zero-width-space → must be wrapped), call `_list_tasks` via `READONLY_TOOLS["dlw_list_tasks"].run(session, principal)`, assert `result["items"][0]["error_message"]` starts with `<external_content`.

  ```python
  # Add near existing tests for _list_tasks (or after _get_task test)
  async def test_list_tasks_sanitizes_error_message_via_choke_point(session, ...):
      """Regression: dlw_list_tasks now sanitizes items[].error_message at the
      choke point (was a latent inv-19 gap before SP4e follow-on)."""
      # Seed: a task with a malicious error_message containing zero-width space.
      # ... (mirror existing fixture pattern) ...
      result = await call_tool(session, principal, "dlw_list_tasks", {})
      assert result["items"][0]["error_message"].startswith("<external_content")
  ```

  **Note:** the existing `_get_task` test may call `_get_task` directly (bypassing `call_tool`). The choke point is in `call_tool`, NOT in `_get_task` itself — so direct calls to `_get_task` will NOT have the inline sanitize anymore. **Update the existing `_get_task` test** to call via `call_tool(...)` instead of `READONLY_TOOLS["dlw_get_task"].run(...)` if it's currently calling `.run()` directly. Same for `_get_task_events`. Read the existing tests carefully to know which one to update.

  Likewise: if there's a test asserting the inline sanitization happens in `_get_task`, that test will break after removing the inline call. Either (a) rewrite to go through `call_tool`, or (b) delete and replace with a choke-point-based assertion.

- [ ] **Step 3: verify FAIL** — `uv run pytest tests/ai/test_tools.py -v` — new test fails (no choke point yet), existing tests for `_get_task.error_message` might still pass IF they call `_get_task` directly with the inline call still in place (we haven't removed it yet).

- [ ] **Step 4 (add `external_fields` to Tool dataclass):** in `src/dlw/ai/tools.py`, modify the `Tool` dataclass:

  ```python
  from dataclasses import dataclass, field  # ensure `field` is imported

  @dataclass
  class Tool:
      name: str
      description: str
      input_schema: dict
      run: Callable[..., Awaitable[dict]]
      # SP4e follow-on: declarative external-content field paths. The choke
      # point in service.call_tool applies sanitize_external() to each path
      # before the tool result is attached to a tool_result event.
      # Path syntax:
      #   "field"           — top-level string field
      #   "field[].nested"  — iterate items in `field` (list of dicts),
      #                        sanitize nested string on each
      # Semantics:
      #   None  → tool fully pre-wraps output; choke point applies NO
      #           sanitization (avoids double-wrap; used by hf_* tools).
      #   []    → tool returns only internal data; no fields to sanitize.
      #   [...] → choke point sanitizes those paths IF the value is not
      #           already boundary-wrapped (idempotency guard).
      external_fields: list[str] | None = field(default_factory=list)
  ```

  If `Tool` is also defined in `write_tools.py` separately, mirror the change. If `write_tools.py` imports `Tool` from `tools.py`, no extra change needed.

- [ ] **Step 5 (per-tool declarations + remove inline sanitize):** in `src/dlw/ai/tools.py`'s `READONLY_TOOLS` dict literal:

  ```python
  READONLY_TOOLS: dict[str, Tool] = {
      "dlw_list_tasks": Tool(
          name="dlw_list_tasks",
          description=...,
          input_schema=...,
          run=_list_tasks,
          external_fields=["items[].error_message"],
      ),
      "dlw_get_task": Tool(
          ...,
          run=_get_task,
          external_fields=["error_message"],
      ),
      "dlw_get_task_events": Tool(
          ...,
          run=_get_task_events,
          external_fields=["items[].message"],
      ),
      "dlw_quota_current": Tool(
          ...,
          # external_fields defaults to []
      ),
      "hf_api_metadata": Tool(
          ...,
          external_fields=None,  # pre-wraps files_sanitized
      ),
      "hf_model_card": Tool(
          ...,
          external_fields=None,  # pre-wraps sanitized
      ),
  }
  ```

  **Then remove the inline `sanitize_external` calls** in `_get_task` and `_get_task_events`. Specifically:

  In `_get_task` (~line 62):
  ```python
  # REMOVE:
  if d.get("error_message"):
      d["error_message"] = sanitize_external(
          d["error_message"], source="executor").text
  ```

  In `_get_task_events` (~line 83):
  ```python
  # REMOVE:
  if m.get("message"):
      m["message"] = sanitize_external(m["message"], source="event").text
  ```

  **Note about `source` attribute drift**: the inline calls used `source="executor"` and `source="event"`. After removal, the choke point passes `source=f"tool:{tool.name}"` — so the new boundary attribute reads `source="tool:dlw_get_task"` and `source="tool:dlw_get_task_events"`. This is a **deliberate change** — the new source values are more informative (caller-tool, not just "executor"). Tests that asserted the old source string MUST be updated.

  If `from dlw.ai.sanitize import sanitize_external` is now unused in `tools.py`, remove the import (ruff will flag F401).

- [ ] **Step 6 (write_tools.py):** if `write_tools.py` has its own `Tool` definition, mirror Step 4 there too. If it imports `Tool` from `tools.py`, no dataclass change. Either way, declare `external_fields=[]` (or rely on default) for both write tools — they return internal data only.

- [ ] **Step 7 (wire choke point in service.py):** in `src/dlw/ai/service.py::call_tool`, after `result = await tool.run(...)` and before `return result`:

  ```python
  # SP4e follow-on: structural sanitize choke point.
  if tool.external_fields:
      from dlw.ai._sanitize_apply import apply_external_fields
      apply_external_fields(
          result, tool.external_fields, source=f"tool:{tool.name}")
  ```

  Lazy import keeps module load minimal and matches the project's existing pattern (e.g., the `count_stuck_local_orphans` lazy import in `api/executors.py`).

  **Caveat**: if `call_tool` returns multiple paths (success vs error), make sure the choke point only runs on success results. The standard pattern is:
  ```python
  try:
      result = await tool.run(...)
  except Exception as e:
      result = {"error": str(e)}
      # ... audit ...
      return result
  # success path:
  if tool.external_fields:
      ...
  return result
  ```
  If `call_tool` returns `{"error": str(e)}` on error, the `error` field is a Python-formatted exception string — internal, not external content, no sanitize needed. (If a future enhancement wanted to sanitize error strings too, add `"error"` to a synthetic always-sanitized list — out of scope here.)

- [ ] **Step 8: verify PASS** — `uv run pytest tests/ai/ -v` — all pass including the new `dlw_list_tasks` regression. If existing `_get_task` / `_get_task_events` tests broke (because they called `.run()` directly bypassing the choke point), update them to call `call_tool(...)` instead. If tests asserted the old `source="executor"` / `source="event"` strings, update to `source="tool:dlw_get_task"` / `source="tool:dlw_get_task_events"`.

- [ ] **Step 9 (full backend gate):** `uv run pytest -q` — all pass (no test outside `tests/ai/` should be affected). `uv run python tools/lint_invariants.py --strict` OK.

- [ ] **Step 10: tidy + commit:**
  ```bash
  uv run ruff check --select I001 --fix src/dlw/ai/tools.py src/dlw/ai/write_tools.py src/dlw/ai/service.py tests/ai/test_tools.py
  git add src/dlw/ai/tools.py src/dlw/ai/write_tools.py src/dlw/ai/service.py tests/ai/test_tools.py
  git commit -m "feat(sp4e): wire Tool.external_fields + choke point in call_tool"
  ```

### Task 4: M2 full gate
- [ ] `cd "D:/download_weights" && uv run pytest -q` — full suite green. `uv run python tools/lint_invariants.py --strict` OK. No commit.

---

## Self-Review

- **Spec coverage:** `Tool.external_fields` dataclass field → Task 3 Step 4 ✓; per-tool declarations including `None` for hf_* → Step 5 ✓; choke point in `call_tool` → Step 7 ✓; idempotency via `_is_wrapped` → Helper Step 3 ✓; `dlw_list_tasks` gap fix → declaration `["items[].error_message"]` + regression test ✓; removal of inline calls → Step 5 ✓.
- **Placeholder scan:** all code blocks concrete. Step 2 of Task 3 has a "mirror existing fixture pattern" note — that's TDD guidance to match existing test scaffolding, not a TODO. Implementer must read the existing test file to write a compatible test.
- **Type consistency:** `Tool.external_fields: list[str] | None`; `apply_external_fields(dict, list[str], *, str) -> None`; `_is_wrapped(str) -> bool`; choke point passes `source=f"tool:{tool.name}"` (str).
- **Open risks for reviewers:** (a) `Tool` dataclass might be re-defined in `write_tools.py` rather than imported — Step 6 covers both cases. (b) Removing inline sanitize in `_get_task` changes `source` attribute from `"executor"` to `"tool:dlw_get_task"` — tests must be updated; this is documented in Step 8. (c) The choke point only fires on success path; error path returns `{"error": ...}` which is internal — no sanitize needed (documented in Step 7 caveat). (d) Field defaults: `default_factory=list` not `None` so the SAFE default is "explicit nothing to sanitize"; tools that pre-wrap MUST explicitly set `external_fields=None` (typo or omission → `[]` → choke point no-op, which is the correct behavior for already-pre-wrapped tools too, just slightly less efficient). (e) The recursive helper's "head ends with `[]` but rest is empty" edge case is silent no-op — documented in Step 3 note.
