"""provider 429 backoff and delayed automatic retry (#105)

Revision ID: 0031
Revises: 0030
Create Date: 2026-11-06 15:00:00

`provider_backoffs`: a model provider is paused until `until` after a 429 (workers using it
claim nothing). `validation_jobs.run_after`: a queued automatic retry is not claimed before it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | Sequence[str] | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "validation_jobs", sa.Column("run_after", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_table(
        "provider_backoffs",
        sa.Column("provider", sa.String(length=30), nullable=False),
        sa.Column("until", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("provider", name=op.f("pk_provider_backoffs")),
    )


def downgrade() -> None:
    op.drop_table("provider_backoffs")
    op.drop_column("validation_jobs", "run_after")
