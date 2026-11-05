"""worker readiness snapshot, report readable time, model_usage call details (#105)

Revision ID: 0030
Revises: 0029
Create Date: 2026-11-06 14:00:00

`validation_jobs.readiness`: what the worker actually ran with (engine version/commit, configured
models, critic switch, limits, browser), no secrets. `validation_jobs.report_readable_at`: when
the persisted result became readable. `model_usage` keeps role, configured model, latency and
Retry-After, which outlive the raw result after the retention purge. All nullable: older rows
stay unknown.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0030"
down_revision: str | Sequence[str] | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "validation_jobs",
        sa.Column("report_readable_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "validation_jobs",
        sa.Column("readiness", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("model_usage", sa.Column("role", sa.String(length=20), nullable=True))
    op.add_column(
        "model_usage", sa.Column("configured_model", sa.String(length=200), nullable=True)
    )
    op.add_column("model_usage", sa.Column("latency_ms", sa.Integer(), nullable=True))
    op.add_column("model_usage", sa.Column("retry_after_seconds", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("model_usage", "retry_after_seconds")
    op.drop_column("model_usage", "latency_ms")
    op.drop_column("model_usage", "configured_model")
    op.drop_column("model_usage", "role")
    op.drop_column("validation_jobs", "readiness")
    op.drop_column("validation_jobs", "report_readable_at")
