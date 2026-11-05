"""origin ownership verification

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-05 00:00:00

Backfill: every existing program's *current* revision origin is marked verified (now()), so
programs already running (the demo) keep accepting reports. New origins start unverified.
"""

import secrets
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    table = op.create_table(
        "origin_verifications",
        sa.Column("program_id", sa.String(length=32), nullable=False),
        sa.Column("origin", sa.String(length=2048), nullable=False),
        sa.Column("token", sa.String(length=43), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["program_id"],
            ["programs.id"],
            name=op.f("fk_origin_verifications_program_id_programs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("program_id", "origin", name=op.f("pk_origin_verifications")),
    )
    current = op.get_bind().execute(
        sa.text(
            "SELECT p.id, r.base_url FROM programs p JOIN program_scope_revisions r"
            " ON r.program_id = p.id AND r.revision = p.current_revision"
        )
    )
    rows = [
        {
            "program_id": program_id,
            # Same as app.services.origin_verification.origin_of (base_url is origin + "/").
            "origin": base_url.rstrip("/"),
            "token": secrets.token_urlsafe(32),
            "verified_at": sa.func.now(),
        }
        for program_id, base_url in current
    ]
    for row in rows:
        op.execute(table.insert().values(**row))


def downgrade() -> None:
    op.drop_table("origin_verifications")
