"""v2.1 Sprint 1 — add sla_tier to tenants

Adds a per-tenant SLA tier column used by the scheduler to weight
priority and by the quota service for admission control.

Tiers (high-to-low priority):
  - critical  : weight 4, never starved, admission always
  - standard  : weight 2, default, admission unless system >90% busy
  - bulk      : weight 1, may be denied admission under load, may starve
                (but with a 30-min starvation timeout — handled in code)

Revision ID: c2d3e4f5a6b7
Revises: a1b2c3d4e5f6
Create Date: 2026-05-26 (v2.1 sprint 1)
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c2d3e4f5a6b7"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "sla_tier", sa.String(16), nullable=False, server_default="standard",
        ),
    )
    op.create_check_constraint(
        "ck_tenants_sla_tier",
        "tenants",
        "sla_tier IN ('critical', 'standard', 'bulk')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_tenants_sla_tier", "tenants", type_="check")
    op.drop_column("tenants", "sla_tier")
