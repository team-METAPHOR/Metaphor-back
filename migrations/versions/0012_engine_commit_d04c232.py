"""engine_commit default d04c232

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-05 16:00:00

New jobs record the engine they run on (core_engine d04c232: plan checks before execution,
optional critic review). Existing rows keep the commit they ran with.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("validation_jobs", "engine_commit", server_default="d04c232")


def downgrade() -> None:
    op.alter_column("validation_jobs", "engine_commit", server_default="2ccf627")
