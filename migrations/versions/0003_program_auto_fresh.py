"""program auto_fresh_retries

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-05 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "programs",
        sa.Column("auto_fresh_retries", sa.Integer(), server_default="1", nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_programs_auto_fresh_retries"), "programs", "auto_fresh_retries BETWEEN 0 AND 2"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_programs_auto_fresh_retries"), "programs", type_="check")
    op.drop_column("programs", "auto_fresh_retries")
