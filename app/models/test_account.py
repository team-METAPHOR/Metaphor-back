from datetime import datetime

from sqlalchemy import CheckConstraint, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.columns import created_at, fk, updated_at


class ProgramTestAccount(Base):
    """The program's test login (one per program), sealed for the worker.

    Program-level, not part of a scope revision: replacing or deleting the credential must not
    mint a new revision, and the worker reads the current row at run time. Only the worker's
    private key opens `ciphertext` (app/core/test_account_crypto.py); the API keeps a masked
    login id for display.
    """

    __tablename__ = "program_test_accounts"
    __table_args__ = (CheckConstraint("version >= 1", name="version_positive"),)

    program_id: Mapped[str] = fk("programs.id", ondelete="CASCADE", primary_key=True)
    # Bumped on every replace; bound into the ciphertext's associated data.
    version: Mapped[int] = mapped_column(Integer)
    key_id: Mapped[str] = mapped_column(String(64))
    wrapped_key: Mapped[bytes] = mapped_column(LargeBinary)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    login_id_masked: Mapped[str] = mapped_column(String(300))
    login_path: Mapped[str | None] = mapped_column(String(2048))
    updated_by: Mapped[str | None] = fk("users.id", nullable=True, index=False)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()
