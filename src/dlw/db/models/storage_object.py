"""Global-dedup storage objects (Phase 3 SP3; doc 06 §3.1, INVARIANT 14)."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from dlw.db.base import Base


class StorageObject(Base):
    __tablename__ = "storage_objects"
    __table_args__ = (
        UniqueConstraint("tenant_id", "storage_id", "sha256"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tenants.id"), nullable=False)
    storage_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    refcount: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_referenced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)


class StoragePhysicalKey(Base):
    """Phase 4: durable ledger of every physical object key written (download +
    inherit), decoupled from the dedup storage_objects row. Enables reclamation
    of inherit-copied new-revision keys that storage_objects never tracked."""
    __tablename__ = "storage_physical_keys"
    __table_args__ = (
        UniqueConstraint("tenant_id", "storage_id", "storage_key",
                         name="uq_phys_key_tenant_storage_key"),
        Index("idx_phys_key_gc", "tenant_id", "storage_id", "sha256"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tenants.id"), nullable=False)
    storage_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)


class SubtaskObjectRef(Base):
    __tablename__ = "subtask_object_refs"

    subtask_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("file_subtasks.id", ondelete="CASCADE"),
        primary_key=True)
    object_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("storage_objects.id"), primary_key=True)
