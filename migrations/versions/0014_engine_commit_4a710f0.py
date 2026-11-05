"""engine_commit default 4a710f0

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-05 16:00:00

New jobs record the engine they run on (core_engine 4a710f0: diagnosis, masked video,
Playwright test export, condition comparison). Existing rows keep the commit they ran with.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("validation_jobs", "engine_commit", server_default="4a710f0")


def downgrade() -> None:
    op.alter_column("validation_jobs", "engine_commit", server_default="d04c232")
