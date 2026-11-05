"""engine_commit default 38bbf5b

Revision ID: 0032
Revises: 0031
Create Date: 2026-11-06 03:00:00

New jobs record core_engine 38bbf5b (0.7.1: critic reviews drop element geometry so they fit
provider size limits; provider 413 is reported as MODEL_REQUEST_TOO_LARGE).
Existing rows keep the commit they ran with.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0032"
down_revision: str | Sequence[str] | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("validation_jobs", "engine_commit", server_default="38bbf5b")


def downgrade() -> None:
    op.alter_column("validation_jobs", "engine_commit", server_default="426b3da")
