"""report number

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-05 12:00:00

Per-org human report number (BUG-<number>): reports.number, UNIQUE(org_id, number), and the
org counter organizations.report_seq. Backfill numbers existing reports per org in
(created_at, id) order and sets report_seq to each org's max (0 for orgs without reports).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("report_seq", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column("reports", sa.Column("number", sa.Integer(), nullable=True))
    op.execute(
        """
        UPDATE reports SET number = numbered.n
        FROM (
            SELECT id, row_number() OVER (PARTITION BY org_id ORDER BY created_at, id) AS n
            FROM reports
        ) AS numbered
        WHERE reports.id = numbered.id
        """
    )
    op.execute(
        """
        UPDATE organizations SET report_seq = counts.max_number
        FROM (SELECT org_id, max(number) AS max_number FROM reports GROUP BY org_id) AS counts
        WHERE organizations.id = counts.org_id
        """
    )
    op.alter_column("reports", "number", nullable=False)
    op.create_unique_constraint("uq_reports_org_number", "reports", ["org_id", "number"])


def downgrade() -> None:
    op.drop_constraint("uq_reports_org_number", "reports", type_="unique")
    op.drop_column("reports", "number")
    op.drop_column("organizations", "report_seq")
