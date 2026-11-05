"""engine_commit default eeaa5ea

Revision ID: 0041
Revises: 0040
Create Date: 2026-11-07 09:00:00

New jobs record core_engine eeaa5ea (0.8.9: plan-repair requests send only the elements that
vanished in `replay_pages`, so the request no longer exceeds the model's size limit).
Existing rows keep the commit they ran with.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0041"
down_revision: str | Sequence[str] | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("validation_jobs", "engine_commit", server_default="eeaa5ea")


def downgrade() -> None:
    op.alter_column("validation_jobs", "engine_commit", server_default="cd98619")
