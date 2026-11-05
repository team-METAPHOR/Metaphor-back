"""program test condition registry

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-05 18:00:00

Per-program registered run conditions (browser, viewport, response delay, test context,
video, reset adapter, operator-bound session reference). Jobs snapshot the selection.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "program_test_conditions",
        sa.Column("program_id", sa.String(length=32), nullable=False),
        sa.Column("condition_id", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("browser_name", sa.String(length=20), server_default="chromium", nullable=False),
        sa.Column("width", sa.Integer(), server_default="1280", nullable=False),
        sa.Column("height", sa.Integer(), server_default="800", nullable=False),
        sa.Column("response_delay_ms", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "test_context_id", sa.String(length=100), server_default="anonymous", nullable=False
        ),
        sa.Column("record_video", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "reset_adapter", sa.String(length=64), server_default="stateless", nullable=False
        ),
        sa.Column("session_secret_ref", sa.String(length=100), nullable=True),
        sa.Column("created_by", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "browser_name IN ('chromium', 'firefox', 'webkit')",
            name=op.f("ck_program_test_conditions_browser_name"),
        ),
        sa.CheckConstraint(
            "response_delay_ms BETWEEN 0 AND 2000",
            name=op.f("ck_program_test_conditions_response_delay_ms"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_program_test_conditions_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["program_id"],
            ["programs.id"],
            name=op.f("fk_program_test_conditions_program_id_programs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "program_id", "condition_id", name=op.f("pk_program_test_conditions")
        ),
    )


def downgrade() -> None:
    op.drop_table("program_test_conditions")
