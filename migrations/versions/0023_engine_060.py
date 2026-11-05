"""engine_commit default 2c21df2

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-06 10:00:00

New jobs record core_engine 2c21df2 (0.6.0: queued browser requests, non-fatal blocked
subresources and 4xx, opt-in approved test writes with a server reset). Existing rows keep the
commit they ran with.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0023"
down_revision: str | Sequence[str] | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("validation_jobs", "engine_commit", server_default="2c21df2")


def downgrade() -> None:
    op.alter_column("validation_jobs", "engine_commit", server_default="093535d")
