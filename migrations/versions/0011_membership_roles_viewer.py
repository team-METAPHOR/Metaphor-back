"""membership roles admin / developer / viewer

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-05 13:00:00

Roles become admin / developer / viewer (was admin / reviewer / developer), on memberships and
org_invites. Upgrade: reviewer → developer (developer now has the old reviewer powers);
developer and admin are unchanged.

Downgrade (lossy): developer → reviewer, so members keep their write powers, then
viewer → developer, the least-privileged old role. Upgrade-then-downgrade therefore turns
original developers into reviewers.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("memberships", "org_invites")


def _swap(values: tuple[str, ...], steps: tuple[tuple[str, str], ...]) -> None:
    """Drop the CHECK, remap roles in order, re-create the CHECK with `values`."""
    quoted = ", ".join(f"'{value}'" for value in values)
    for table in TABLES:
        op.drop_constraint(op.f(f"ck_{table}_role"), table, type_="check")
        for old, new in steps:
            op.execute(
                sa.text(f"UPDATE {table} SET role = :new WHERE role = :old").bindparams(
                    old=old, new=new
                )
            )
        op.create_check_constraint(op.f(f"ck_{table}_role"), table, f"role IN ({quoted})")


def upgrade() -> None:
    _swap(("admin", "developer", "viewer"), (("reviewer", "developer"),))


def downgrade() -> None:
    _swap(
        ("developer", "reviewer", "admin"),
        (("developer", "reviewer"), ("viewer", "developer")),
    )
