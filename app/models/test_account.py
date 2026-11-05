from datetime import datetime

from sqlalchemy import ARRAY, CheckConstraint, Integer, LargeBinary, String, Text, text
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
    # Where the login form posts (default: login_path) and how the engine confirms success.
    submit_path: Mapped[str | None] = mapped_column(String(2048))
    success_path: Mapped[str | None] = mapped_column(String(2048))
    success_selector: Mapped[str | None] = mapped_column(String(256))
    # Exact paths whose GET 401 is the app's normal signed-out probe while signing in
    # (core_engine 0.6.0 LoginOptions.unauthenticated_paths, e.g. `/api/auth/me`).
    unauthenticated_paths: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("'{}'"), nullable=False
    )
    updated_by: Mapped[str | None] = fk("users.id", nullable=True, index=False)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    @property
    def login_ready(self) -> bool:
        """The engine can sign in with it: a login path and an explicit success signal."""
        return bool(self.login_path and (self.success_path or self.success_selector))


def login_applies(test_context_id: str, session_secret_ref: str | None) -> bool:
    """Whether a run in this context signs in with the saved account. core_engine 0.4.0 needs
    an explicit (non-anonymous) test context and refuses to mix a login with fixed headers,
    so an operator-provisioned session wins."""
    return test_context_id != "anonymous" and session_secret_ref is None
