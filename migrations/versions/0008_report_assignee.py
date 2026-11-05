"""report assignee

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-05 11:34:34

Nullable reports.assignee_id → users. Deactivated members keep their assignments (no cascade).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("reports", sa.Column("assignee_id", sa.String(length=32), nullable=True))
    op.create_index(op.f("ix_reports_assignee_id"), "reports", ["assignee_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_reports_assignee_id_users"),
        "reports",
        "users",
        ["assignee_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_reports_assignee_id_users"), "reports", type_="foreignkey")
    op.drop_index(op.f("ix_reports_assignee_id"), table_name="reports")
    op.drop_column("reports", "assignee_id")
