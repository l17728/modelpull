"""DownloadTask + FileSubTask models (architecture §4.2 + §4.7.1).

v2.0 schema includes Phase 2/v2.1 fence-token columns as nullable here;
logic that uses them comes in Week 2's plan.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from dlw.db.base import Base


class DownloadTask(Base):
    __tablename__ = "download_tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("tenants.id"), nullable=False)
    project_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("projects.id"), nullable=False)
    owner_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    repo_id: Mapped[str] = mapped_column(String(256), nullable=False)
    revision: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("storage_backends.id"), nullable=False
    )
    path_template: Mapped[str] = mapped_column(String(512), nullable=False)
    priority: Mapped[int] = mapped_column(SmallInteger, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    is_simulation: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    download_bytes_limit: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    upgrade_from_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Phase 3 SP2: multi-source columns
    source_strategy: Mapped[str] = mapped_column(String(32), default="auto_balance", nullable=False)
    source_blacklist: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    trust_non_hf_sha256: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # ORM relationship — Phase 1 Week 3 UI scaffold consumes via
    # selectinload(DownloadTask.subtasks) in api/tasks.get_task.
    # Cascade is intentionally narrow: the FK already has ondelete=CASCADE,
    # so DB-level cleanup handles deletion. Adding ORM-level "delete-orphan"
    # would risk scheduling orphan deletes if any code path triggers a lazy
    # load of an empty in-memory subtasks collection on a flushed parent.
    # "delete" + passive_deletes=True makes the ORM defer to that ON DELETE
    # CASCADE on session.delete(task) instead of UPDATE-ing loaded children's
    # task_id to NULL (NOT NULL violation) — required by DELETE /tasks. Plain
    # "delete" (NOT "delete-orphan") only fires when the parent itself is
    # deleted, so it carries none of the orphan-on-lazy-load risk above.
    subtasks: Mapped[list[FileSubTask]] = relationship(
        "FileSubTask",
        back_populates="task",
        cascade="save-update, merge, delete",
        passive_deletes=True,
        lazy="select",
    )


class FileSubTask(Base):
    __tablename__ = "file_subtasks"
    __table_args__ = (UniqueConstraint("task_id", "filename"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("download_tasks.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)  # denormalized for query
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    expected_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actual_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)

    executor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Fence-token columns (Phase 2 wires logic; Phase 1 leaves nullable)
    executor_epoch: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    assignment_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    # Phase 2 W1 recovery: timestamps used by reclaim + recovery_routine.
    # multipart_started_at is set when the executor begins multipart_upload;
    # assigned_at is set on claim_one_subtask (used by recovery threshold);
    # last_heartbeat_seen_at is updated when controller sees this subtask in a
    # heartbeat report (P2-W2 will populate it; P2-W1 just adds the column).
    multipart_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    assigned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_heartbeat_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # W2b2 §3.8: written when sub flips to paused_external or paused_disk_full.
    # Used by sweep_paused_external's quiet-window threshold.
    last_paused_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    chunks_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunks_completed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    bytes_downloaded: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    multipart_upload_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # Phase 1 W4: object key in S3 / OSS (e.g., "phase1/owner/repo/rev/file.bin")
    # Populated by complete_subtask when executor reports it. Used for debugging
    # and Phase 2 multipart resume (joined with multipart_upload_id).
    s3_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Phase 3 SP2: multi-source columns
    source_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_chunked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Phase 3 SP3: incremental download
    inherit_from_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    task: Mapped[DownloadTask] = relationship(
        "DownloadTask",
        back_populates="subtasks",
        lazy="select",
    )
