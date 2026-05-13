#!/usr/bin/env python3
"""
lint_no_direct_status_write.py — forbid Python attribute writes to
Executor.status outside the state machine module (W2a §3.7).

Heuristic: AST-walks every .py file under `src/dlw/` and flags any
`Assign` or `AugAssign` whose target is an attribute named `status`
on a value resembling an Executor row (named `ex`, `executor`,
`e_self`, `e_other`, etc.).

ALLOWED_FILES below are exempt — currently only state_machine.py.
SQL-builder kwargs (`pg_insert.values(status=...)`, `.update().values(
status=...)`) are NOT attribute assignments and are correctly outside
the pattern.

Exit codes:
  0 — clean
  1 — at least one violation
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_ROOT = ROOT / "src" / "dlw"
ALLOWED_FILES = {
    SCAN_ROOT / "services" / "state_machine.py",
}
EXECUTOR_NAME_HINTS = {"ex", "executor", "e_self", "e_other", "e"}


def _is_status_attribute_target(target: ast.expr) -> bool:
    if not isinstance(target, ast.Attribute):
        return False
    if target.attr != "status":
        return False
    if isinstance(target.value, ast.Name) and target.value.id in EXECUTOR_NAME_HINTS:
        return True
    # ex.row.status or similar nested attribute chain: be permissive — flag any
    # `.status` write whose root name hint matches; ignore other nesting.
    cur = target.value
    while isinstance(cur, ast.Attribute):
        cur = cur.value
    if isinstance(cur, ast.Name) and cur.id in EXECUTOR_NAME_HINTS:
        return True
    return False


def scan_file(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if _is_status_attribute_target(tgt):
                    hits.append((node.lineno, ast.unparse(tgt)))
        elif isinstance(node, ast.AugAssign):
            if _is_status_attribute_target(node.target):
                hits.append((node.lineno, ast.unparse(node.target)))
    return hits


def main() -> int:
    violations: list[tuple[Path, int, str]] = []
    for py in SCAN_ROOT.rglob("*.py"):
        if py in ALLOWED_FILES:
            continue
        for line, text in scan_file(py):
            violations.append((py.relative_to(ROOT), line, text))

    if violations:
        print("Direct Executor.status writes found (route through state_machine.transition_executor):")
        for path, line, text in violations:
            print(f"  {path}:{line}  {text} = ...")
        return 1
    print("No direct Executor.status writes in src/dlw/. OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
