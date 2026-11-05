"""report_duplicates: each merged widget duplicate's own redacted text, context and screenshot

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-05 17:00:00

Until now a duplicate only bumped reports.duplicate_count and its content was dropped. Existing
duplicates stay counted with no stored rows.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "report_duplicates",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("report_id", sa.String(length=32), nullable=False),
        sa.Column("org_id", sa.String(length=32), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("client_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("input_artifact_id", sa.String(length=32), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_report_duplicates_org_id_organizations"),
        ),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["reports.id"],
            name=op.f("fk_report_duplicates_report_id_reports"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_report_duplicates")),
    )
    op.create_index(
        op.f("ix_report_duplicates_org_id"), "report_duplicates", ["org_id"], unique=False
    )
    op.create_index(
        "ix_report_duplicates_report_received",
        "report_duplicates",
        ["report_id", "received_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_report_duplicates_report_received", table_name="report_duplicates")
    op.drop_index(op.f("ix_report_duplicates_org_id"), table_name="report_duplicates")
    op.drop_table("report_duplicates")
