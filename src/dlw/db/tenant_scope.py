"""Mandatory tenant-scoping helper (Phase 3 SP1, Invariant 8).

EVERY business-table query MUST go through this so a forgotten WHERE
tenant_id= can't leak cross-tenant rows. This is the runtime backstop;
the source AST lint tools/lint_invariants.py is the structural one."""
from __future__ import annotations

from typing import Any

from dlw.auth.principal import Principal


def tenant_filtered(stmt: Any, model: Any, principal: Principal) -> Any:
    return stmt.where(model.tenant_id == principal.tenant_id)
