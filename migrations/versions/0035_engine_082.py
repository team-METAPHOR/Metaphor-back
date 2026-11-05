"""engine_commit default 308a9c0

Revision ID: 0035
Revises: 0034
Create Date: 2026-11-06 21:00:00

New jobs record core_engine 308a9c0 (0.8.2: a reported literal typed right before a reported
click is a grounded precondition, a complete reporter_activity recording is replayed
deterministically with the model plan as runtime fallback, and an unlinked start screen can be
reached through the site root and the named item).
Existing rows keep the commit they ran with.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0035"
down_revision: str | Sequence[str] | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("validation_jobs", "engine_commit", server_default="308a9c0")


def downgrade() -> None:
    op.alter_column("validation_jobs", "engine_commit", server_default="1300f8c")
