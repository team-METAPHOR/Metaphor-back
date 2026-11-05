"""engine_commit default 093535d

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-05 23:00:00

New jobs record core_engine 093535d (0.5.0: opt-in masked live preview stream). Existing rows
keep the commit they ran with.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0021"
down_revision: str | Sequence[str] | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("validation_jobs", "engine_commit", server_default="093535d")


def downgrade() -> None:
    op.alter_column("validation_jobs", "engine_commit", server_default="0f833d2")
