"""casbin policy storage (Phase 3 SP1). Authz infrastructure table — not a
business/data table, so it intentionally has no tenant_id (like
alembic_version). NOTE: there is no CI information_schema tenant_id scan in
this repo (the Invariant-8 CI gate is the source AST lint
tools/lint_invariants.py, which does not inspect DB tables), so no allowlist
entry is needed — this comment is documentation only."""
from __future__ import annotations

from sqlalchemy import BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from dlw.db.base import Base


class CasbinRule(Base):
    __tablename__ = "casbin_rule"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ptype: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    v0: Mapped[str | None] = mapped_column(String(256), nullable=True)
    v1: Mapped[str | None] = mapped_column(String(256), nullable=True)
    v2: Mapped[str | None] = mapped_column(String(256), nullable=True)
    v3: Mapped[str | None] = mapped_column(String(256), nullable=True)
    v4: Mapped[str | None] = mapped_column(String(256), nullable=True)
    v5: Mapped[str | None] = mapped_column(String(256), nullable=True)
