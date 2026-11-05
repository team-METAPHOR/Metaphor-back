"""live_tickets: one-time tickets for direct wss:// live preview connections

Revision ID: 0027
Revises: 0026
Create Date: 2026-11-06 18:00:00

Issue #107. The dashboard's host-only `sid` cookie never reaches the API host, so a direct
WebSocket offers a 30 s single-use ticket (issued over the cookie-authenticated REST proxy) in
Sec-WebSocket-Protocol. Only sha256(ticket) is stored; deleting the session deletes its tickets.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | Sequence[str] | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "live_tickets",
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(32), nullable=False),
        sa.Column("org_id", sa.String(32), nullable=False),
        sa.Column("job_id", sa.String(32), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name=op.f("fk_live_tickets_session_id_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_live_tickets_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_live_tickets_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["validation_jobs.id"],
            name=op.f("fk_live_tickets_job_id_validation_jobs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("token_hash", name=op.f("pk_live_tickets")),
    )
    op.create_index(op.f("ix_live_tickets_session_id"), "live_tickets", ["session_id"])
    op.create_index(op.f("ix_live_tickets_org_id"), "live_tickets", ["org_id"])
    op.create_index(op.f("ix_live_tickets_job_id"), "live_tickets", ["job_id"])
    op.create_index("ix_live_tickets_user_id_created_at", "live_tickets", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_table("live_tickets")
