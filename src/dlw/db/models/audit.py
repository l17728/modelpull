"""AuditLog model (security §9.1).

Phase 1: schema only. Chain-hash trigger / verifier in Phase 4 plan.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, String, func
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from dlw.db.base import Base


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    tenant_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    actor_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    actor_ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    prev_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    self_hash: Mapped[str] = mapped_column(String(64), nullable=False)
