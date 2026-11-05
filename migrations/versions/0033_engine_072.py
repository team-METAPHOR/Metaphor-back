"""engine_commit default 223ec38

Revision ID: 0033
Revises: 0032
Create Date: 2026-11-06 12:00:00

New jobs record core_engine 223ec38 (0.7.2: the repair schema no longer puts selectors in a
JSON-schema enum, fixing OpenAI 400 invalid_json_schema).
Existing rows keep the commit they ran with.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0033"
down_revision: str | Sequence[str] | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("validation_jobs", "engine_commit", server_default="223ec38")


def downgrade() -> None:
    op.alter_column("validation_jobs", "engine_commit", server_default="38bbf5b")
