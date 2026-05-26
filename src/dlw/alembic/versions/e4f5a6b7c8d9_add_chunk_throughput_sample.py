"""v2.1 Sprint 7 — add chunk_throughput_sample table

Per-chunk completion samples that feed the future Sprint 8 LP solver:
  (executor, source, file_type, bytes, duration_ms, recorded_at)

Why a new table (not reusing source_speed_samples):
  - source_speed_samples is COARSE (one row per probe ≈ minutes apart),
    EWMA-smoothed; good for "is this source healthy"
  - chunk_throughput_sample is FINE (one row per chunk completion ≈ seconds
    apart), raw; needed to build a per-(executor,source,file_type) capacity
    matrix for the LP

Retention is enforced by a daily cleanup task that deletes rows older
than DLW_CHUNK_THROUGHPUT_SAMPLE_RETENTION_DAYS (default 7). No partitioning
needed at this scale — for a typical 100MB chunk and 10 GB/s aggregate
ingest, 7 days at most ~6M rows.

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-05-27 (v2.1 sprint 7)
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e4f5a6b7c8d9"
down_revision: str | None = "d3e4f5a6b7c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chunk_throughput_sample",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("executor_id", sa.String(64), nullable=False),
        sa.Column("source_id", sa.String(32), nullable=False),
        # file_type buckets requests so the LP can learn that
        # safetensors-shard chunks behave differently from tokenizer-json
        # chunks even at the same source. Free-form string (e.g.
        # "safetensors", "json", "bin", "other").
        sa.Column("file_type", sa.String(32), nullable=False,
                  server_default="other"),
        sa.Column("bytes_transferred", sa.BigInteger, nullable=False),
        sa.Column("duration_ms", sa.Integer, nullable=False),
        # tenant_id kept so retention can scope per-tenant if multitenancy
        # ever requires it; for now ALL rows are aggregated by the LP.
        sa.Column("tenant_id", sa.BigInteger,
                  sa.ForeignKey("tenants.id"), nullable=True),
        sa.CheckConstraint("duration_ms > 0", name="ck_cts_duration_positive"),
        sa.CheckConstraint("bytes_transferred >= 0",
                            name="ck_cts_bytes_nonneg"),
        # Retention scan: recorded_at + delete-older-than predicate
        sa.Index("ix_cts_recorded_at", "recorded_at"),
        # LP-solver fact table read pattern: (executor, source, file_type)
        # last N samples
        sa.Index("ix_cts_es_lookup", "executor_id", "source_id",
                  "file_type", "recorded_at"),
    )


def downgrade() -> None:
    op.drop_index("ix_cts_es_lookup", table_name="chunk_throughput_sample")
    op.drop_index("ix_cts_recorded_at", table_name="chunk_throughput_sample")
    op.drop_table("chunk_throughput_sample")
