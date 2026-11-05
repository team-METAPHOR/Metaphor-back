"""engine_commit default 48ec7bb

Revision ID: 0036
Revises: 0035
Create Date: 2026-11-06 23:00:00

New jobs record core_engine 48ec7bb (0.8.3: a full per-minute request window waits within the
runtime instead of blocking the attempt, truncated reporter_activity is replayed from the earliest
recorded page, and plain-text plan targets are grounded to snapshot selectors or sent back for
revision).
Existing rows keep the commit they ran with.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0036"
down_revision: str | Sequence[str] | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("validation_jobs", "engine_commit", server_default="48ec7bb")


def downgrade() -> None:
    op.alter_column("validation_jobs", "engine_commit", server_default="308a9c0")
