"""artifact kinds video and playwright_spec

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-05 18:00:00

Masked reproduction video (WebM) and the exported Playwright test become stored evidence.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = "kind IN ('input_image', 'report_md', 'screenshot', 'json')"
NEW = "kind IN ('input_image', 'report_md', 'screenshot', 'json', 'video', 'playwright_spec')"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_artifacts_kind"), "artifacts", type_="check")
    op.create_check_constraint(op.f("ck_artifacts_kind"), "artifacts", NEW)


def downgrade() -> None:
    op.execute("DELETE FROM artifacts WHERE kind IN ('video', 'playwright_spec')")
    op.drop_constraint(op.f("ck_artifacts_kind"), "artifacts", type_="check")
    op.create_check_constraint(op.f("ck_artifacts_kind"), "artifacts", OLD)
