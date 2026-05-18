"""casbin RBAC enforcer (Phase 3 SP1, tenant-scoped only).

The matcher handles role->obj->act + tenant equality. Project-scoped roles
are deferred (see Task 7 note); require_perm uses enforcer.enforce() -> bool
(no enforce_ex / no scope post-check in SP1)."""
from __future__ import annotations

from pathlib import Path

import casbin
from casbin.persist.adapters import StringAdapter

_DIR = Path(__file__).parent
_MODEL = str(_DIR / "model.conf")


def _base_policy_csv() -> str:
    return (_DIR / "policy.csv").read_text(encoding="utf-8")


def build_enforcer(*, grants: list[tuple[str, str]]) -> casbin.Enforcer:
    """Build an enforcer from model.conf + policy.csv, plus per-subject
    grants (loaded from the DB casbin_rule table at bootstrap; SP1 has
    none, but the wiring is the extension point for a later sub-project)."""
    lines = _base_policy_csv()
    for sub, role in grants:
        lines += f"\ng, {sub}, {role}"
    adapter = StringAdapter(lines)
    return casbin.Enforcer(_MODEL, adapter)


async def load_grants(session) -> list[tuple[str, str]]:
    """Load `g` (subject->role) rows from casbin_rule (empty in SP1)."""
    from sqlalchemy import select

    from dlw.db.models.casbin_rule import CasbinRule

    rows = (await session.execute(
        select(CasbinRule).where(CasbinRule.ptype == "g")
    )).scalars().all()
    return [(r.v0, r.v1) for r in rows if r.v0 and r.v1]
