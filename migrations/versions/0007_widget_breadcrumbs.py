"""widget breadcrumbs toggle

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-05 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "program_widget_settings",
        sa.Column(
            "breadcrumbs_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_column("program_widget_settings", "breadcrumbs_enabled")
