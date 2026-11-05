"""report workflow statuses

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-05 11:30:22

workflow_status becomes received / fixing / dismissed / closed (closed: passing retest only).
Existing rows: in_review → received, accepted → fixing. Adds reports.closed_at.
Downgrade maps fixing → accepted and dismissed → in_review.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHECK = "ck_reports_workflow_status"


def _swap_check(values: tuple[str, ...], mapping: dict[str, str]) -> None:
    op.drop_constraint(op.f(CHECK), "reports", type_="check")
    for old, new in mapping.items():
        op.execute(
            sa.text(
                "UPDATE reports SET workflow_status = :new WHERE workflow_status = :old"
            ).bindparams(old=old, new=new)
        )
    quoted = ", ".join(f"'{value}'" for value in values)
    op.create_check_constraint(op.f(CHECK), "reports", f"workflow_status IN ({quoted})")


def upgrade() -> None:
    op.add_column("reports", sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True))
    _swap_check(
        ("received", "fixing", "dismissed", "closed"),
        {"in_review": "received", "accepted": "fixing"},
    )


def downgrade() -> None:
    _swap_check(
        ("received", "in_review", "accepted", "closed"),
        {"fixing": "accepted", "dismissed": "in_review"},
    )
    op.drop_column("reports", "closed_at")
