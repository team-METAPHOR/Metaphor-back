"""remove external reporters (program invites, submission mode)

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-05 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("program_invites")
    op.drop_constraint(op.f("ck_programs_submission_mode"), "programs", type_="check")
    op.drop_column("programs", "submission_mode")


def downgrade() -> None:
    op.add_column(
        "programs",
        sa.Column(
            "submission_mode", sa.String(length=30), server_default="invite_only", nullable=False
        ),
    )
    op.create_check_constraint(
        op.f("ck_programs_submission_mode"),
        "programs",
        "submission_mode IN ('open_authenticated', 'invite_only')",
    )
    op.create_table(
        "program_invites",
        sa.Column("program_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["program_id"],
            ["programs.id"],
            name=op.f("fk_program_invites_program_id_programs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_program_invites_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("program_id", "user_id", name=op.f("pk_program_invites")),
    )
