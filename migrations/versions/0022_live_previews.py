"""live_previews relay table (UNLOGGED)

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-05 23:30:00

Latest live preview state per running job, written by the worker and read by the API's
WebSocket subscribers (woken with pg_notify('live_preview', job_id)). UNLOGGED: not crash-safe
and not replicated, by design; rows only live while their job runs.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: str | Sequence[str] | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "live_previews",
        sa.Column("job_id", sa.String(32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("stream", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("condition_id", sa.String(64), nullable=True),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "messages",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("frame", sa.LargeBinary(), nullable=True),
        sa.Column("frame_sequence", sa.Integer(), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["job_id"], ["validation_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("job_id"),
        prefixes=["UNLOGGED"],
    )


def downgrade() -> None:
    op.drop_table("live_previews")
