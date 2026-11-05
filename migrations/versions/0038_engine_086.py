"""engine_commit default f2d5227

Revision ID: 0038
Revises: 0037
Create Date: 2026-11-06 23:59:00

New jobs record core_engine f2d5227 (0.8.6: same-origin static assets no longer count against
the per-minute and total request budgets, time queued for the rate window extends the step
timeout, and a replay fallback keeps the already-replayed steps).
Existing rows keep the commit they ran with.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0038"
down_revision: str | Sequence[str] | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("validation_jobs", "engine_commit", server_default="f2d5227")


def downgrade() -> None:
    op.alter_column("validation_jobs", "engine_commit", server_default="9e8e5c2")
