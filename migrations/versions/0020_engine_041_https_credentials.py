"""engine_commit default 0f833d2

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-05 22:00:00

New jobs record core_engine 0f833d2 (0.4.1: test credentials and session headers require an
HTTPS target). Existing rows keep the commit they ran with.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0020"
down_revision: str | Sequence[str] | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("validation_jobs", "engine_commit", server_default="0f833d2")


def downgrade() -> None:
    op.alter_column("validation_jobs", "engine_commit", server_default="e396348")
