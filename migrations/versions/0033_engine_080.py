"""새 작업의 엔진 출처 기본값 갱신 (0.8.0, SDK 조작 기록).

Revision ID: 0033
Revises: 0032
Create Date: 2026-11-06

기존 작업의 출처와 결과는 바꾸지 않습니다.
"""

from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("validation_jobs", "engine_commit", server_default="4c6b024")


def downgrade() -> None:
    op.alter_column("validation_jobs", "engine_commit", server_default="38bbf5b")
