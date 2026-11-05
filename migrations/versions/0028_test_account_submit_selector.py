"""program_test_accounts.submit_selector

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-06 14:00:00

The login form's submit control (core_engine LoginOptions.submit_selector) for login pages
where the engine default also matches another button, e.g. a header search form.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: str | Sequence[str] | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "program_test_accounts", sa.Column("submit_selector", sa.String(256), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("program_test_accounts", "submit_selector")
