"""Quota usage models (Phase 3 SP1; security §7.3)."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from dlw.db.base import Base


class UsageRecord(Base):
    __tablename__ = "usage_records"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tenants.id"), nullable=False)
    project_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[int] = mapped_column(BigInteger, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)


class QuotaSnapshot(Base):
    __tablename__ = "quota_snapshots"

    tenant_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tenants.id"), primary_key=True)
    bytes_used_month: Mapped[int] = mapped_column(
        BigInteger, default=0, nullable=False)
    storage_gb_used: Mapped[int] = mapped_column(
        BigInteger, default=0, nullable=False)
    concurrent_tasks: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False)
    last_recomputed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
