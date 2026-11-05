"""report run_blocked (quota parking)

Revision ID: 0011
Revises: 0009
Create Date: 2026-11-05 15:00:00

Nullable reports.run_blocked: "quota" when a report was stored over the daily job limit or the
widget daily cap and has no job yet. Existing rows stay NULL.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHECK = "ck_reports_run_blocked"


def upgrade() -> None:
    op.add_column("reports", sa.Column("run_blocked", sa.String(length=20), nullable=True))
    op.create_check_constraint(op.f(CHECK), "reports", "run_blocked IN ('quota')")


def downgrade() -> None:
    op.drop_constraint(op.f(CHECK), "reports", type_="check")
    op.drop_column("reports", "run_blocked")
