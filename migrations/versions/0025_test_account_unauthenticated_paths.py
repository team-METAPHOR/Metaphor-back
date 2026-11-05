"""program_test_accounts.unauthenticated_paths

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-06 12:00:00

Exact paths whose GET 401 is the app's normal signed-out probe while the engine signs in
(core_engine 0.6.0 LoginOptions.unauthenticated_paths). Existing accounts: none.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0025"
down_revision: str | Sequence[str] | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "program_test_accounts",
        sa.Column(
            "unauthenticated_paths",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("program_test_accounts", "unauthenticated_paths")
