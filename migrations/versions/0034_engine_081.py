"""engine_commit default 1300f8c

Revision ID: 0034
Revises: 0033
Create Date: 2026-11-06 18:00:00

New jobs record core_engine 1300f8c (0.8.1: plans reach the reported screen before acting,
repair can insert one navigation click, and needs_info questions never ask for selectors;
includes 0.8.0 optional reporter_activity context).
Existing rows keep the commit they ran with.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0034"
down_revision: str | Sequence[str] | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("validation_jobs", "engine_commit", server_default="1300f8c")


def downgrade() -> None:
    op.alter_column("validation_jobs", "engine_commit", server_default="223ec38")
