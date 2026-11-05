"""engine_commit default 426b3da

Revision ID: 0029
Revises: 0028
Create Date: 2026-11-06 12:00:00

New jobs record core_engine 426b3da (0.7.0: reload/back actions, url_path/attribute checks,
split transport error codes, model budget events and model failures in the diagnosis).
Existing rows keep the commit they ran with.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0029"
down_revision: str | Sequence[str] | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("validation_jobs", "engine_commit", server_default="426b3da")


def downgrade() -> None:
    op.alter_column("validation_jobs", "engine_commit", server_default="2c21df2")
