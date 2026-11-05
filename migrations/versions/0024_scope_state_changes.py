"""program_scope_revisions.state_changes

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-06 11:00:00

Admin-approved test writes (core_engine 0.6.0 StateChangePolicy) per scope revision. Existing
revisions stay read-only (null).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0024"
down_revision: str | Sequence[str] | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "program_scope_revisions",
        sa.Column("state_changes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("program_scope_revisions", "state_changes")
