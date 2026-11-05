"""engine_commit default 9e8e5c2

Revision ID: 0037
Revises: 0036
Create Date: 2026-11-06 23:59:00

New jobs record core_engine 9e8e5c2 (0.8.5: after a reporter_activity replay the checks are
grounded on the replayed screen and prose check values are rejected, an unobserved readiness
signal no longer blocks when every step completed and both outcomes are observable, and
consecutive identical recorded clicks are collapsed).
Existing rows keep the commit they ran with.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0037"
down_revision: str | Sequence[str] | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("validation_jobs", "engine_commit", server_default="9e8e5c2")


def downgrade() -> None:
    op.alter_column("validation_jobs", "engine_commit", server_default="48ec7bb")
