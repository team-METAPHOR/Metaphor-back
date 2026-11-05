"""program_test_accounts: one sealed test login per program (worker-only private key)

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-05 16:31:50

The login id and password are stored only as RSA-OAEP + AES-256-GCM ciphertext bound to
(program_id, version, key_id); the API never holds a key that can open it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "program_test_accounts",
        sa.Column("program_id", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("key_id", sa.String(length=64), nullable=False),
        sa.Column("wrapped_key", sa.LargeBinary(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("login_id_masked", sa.String(length=300), nullable=False),
        sa.Column("login_path", sa.String(length=2048), nullable=True),
        sa.Column("updated_by", sa.String(length=32), nullable=True),
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
        sa.CheckConstraint("version >= 1", name=op.f("ck_program_test_accounts_version_positive")),
        sa.ForeignKeyConstraint(
            ["program_id"],
            ["programs.id"],
            name=op.f("fk_program_test_accounts_program_id_programs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["users.id"], name=op.f("fk_program_test_accounts_updated_by_users")
        ),
        sa.PrimaryKeyConstraint("program_id", name=op.f("pk_program_test_accounts")),
    )


def downgrade() -> None:
    op.drop_table("program_test_accounts")
