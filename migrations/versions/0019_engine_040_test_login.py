"""engine_commit default e396348, test account login success signal

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-05 20:00:00

New jobs record core_engine e396348 (0.4.0: registered test account form login). The engine
only accepts a login once it is told how success looks, so a saved test account gains an
optional success path and/or success selector, plus the form's submit path.
Existing rows keep the commit they ran with.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | Sequence[str] | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("validation_jobs", "engine_commit", server_default="e396348")
    op.add_column("program_test_accounts", sa.Column("submit_path", sa.String(2048)))
    op.add_column("program_test_accounts", sa.Column("success_path", sa.String(2048)))
    op.add_column("program_test_accounts", sa.Column("success_selector", sa.String(256)))


def downgrade() -> None:
    op.drop_column("program_test_accounts", "success_selector")
    op.drop_column("program_test_accounts", "success_path")
    op.drop_column("program_test_accounts", "submit_path")
    op.alter_column("validation_jobs", "engine_commit", server_default="4a710f0")
