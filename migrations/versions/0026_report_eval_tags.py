"""report_eval_tags: evaluator-only slot per report

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-06 12:00:00

Issue #105. (eval_run_id, case_id, repeat_id) per report, stored apart from `reports` so the
worker (which loads reports.* as engine input) never sees it. One report per slot per org.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | Sequence[str] | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "report_eval_tags",
        sa.Column("report_id", sa.String(32), nullable=False),
        sa.Column("org_id", sa.String(32), nullable=False),
        sa.Column("eval_run_id", sa.String(64), nullable=False),
        sa.Column("case_id", sa.String(64), nullable=False),
        sa.Column("repeat_id", sa.String(64), nullable=False),
        sa.Column("tagged_by", sa.String(32), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["reports.id"],
            name=op.f("fk_report_eval_tags_report_id_reports"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["organizations.id"], name=op.f("fk_report_eval_tags_org_id_organizations")
        ),
        sa.ForeignKeyConstraint(
            ["tagged_by"],
            ["users.id"],
            name=op.f("fk_report_eval_tags_tagged_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("report_id", name=op.f("pk_report_eval_tags")),
        sa.UniqueConstraint(
            "org_id", "eval_run_id", "case_id", "repeat_id", name="uq_report_eval_tags_slot"
        ),
    )


def downgrade() -> None:
    op.drop_table("report_eval_tags")
