"""engine_commit default 3be4d9f

Revision ID: 0039
Revises: 0038
Create Date: 2026-11-07 00:30:00

New jobs record core_engine 3be4d9f (0.8.7: "should change but stays the same" reports are
judged by comparing the same element's text before and after the final action, via relative
`$changed`/`$unchanged` text checks or a fallback when neither literal value matches).
Existing rows keep the commit they ran with.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0039"
down_revision: str | Sequence[str] | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("validation_jobs", "engine_commit", server_default="3be4d9f")


def downgrade() -> None:
    op.alter_column("validation_jobs", "engine_commit", server_default="f2d5227")
