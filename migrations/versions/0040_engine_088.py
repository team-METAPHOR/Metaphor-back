"""engine_commit default cd98619

Revision ID: 0040
Revises: 0039
Create Date: 2026-11-07 06:30:00

New jobs record core_engine cd98619 (0.8.8: every selector check must match an element seen
during the replay, and a visibility/count check whose selector never matched an element in an
attempt blocks it with CHECK_TARGET_NEVER_MATCHED instead of deciding the verdict).
Existing rows keep the commit they ran with.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0040"
down_revision: str | Sequence[str] | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("validation_jobs", "engine_commit", server_default="cd98619")


def downgrade() -> None:
    op.alter_column("validation_jobs", "engine_commit", server_default="3be4d9f")
