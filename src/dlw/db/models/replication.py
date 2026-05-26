"""v2.1 Sprint 4 — ReplicationJob model.

One row per "copy storage_object X to storage_backend Y" work item.
Sprint 4 only adds the schema; Sprint 5 wires the worker."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (BigInteger, CheckConstraint, DateTime, ForeignKey,
                        Index, Integer, String, Text, func, text)
from sqlalchemy.orm import Mapped, mapped_column

from dlw.db.base import Base


class ReplicationJob(Base):
    __tablename__ = "replication_jobs"
    __table_args__ = (
        # Partial unique: a (source, target) pair can have only ONE active
        # (pending/running) job at a time. Re-queueing after a job reaches
        # a terminal state is allowed. Mirrored in Alembic d3e4f5a6b7c8.
        Index(
            "ux_replication_jobs_active_unique",
            "source_object_id", "target_storage_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running')")),
        Index("ix_replication_jobs_tenant_status",
              "tenant_id", "status"),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', "
            "'cancelled', 'skipped_existing')",
            name="ck_replication_jobs_status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tenants.id"), nullable=False)
    source_object_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("storage_objects.id"), nullable=False)
    target_storage_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("storage_backends.id"), nullable=False)
    # pending | running | succeeded | failed | cancelled | skipped_existing
    status: Mapped[str] = mapped_column(String(32), nullable=False,
                                         default="pending",
                                         server_default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    bytes_transferred: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0", nullable=False)
    retry_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
