from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.columns import created_at, fk, id_pk, one_of

MEMBERSHIP_ROLES = ("developer", "reviewer", "admin")


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[str] = id_pk()
    name: Mapped[str] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = created_at()


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = id_pk()
    # Stored lowercased by the auth service.
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    display_name: Mapped[str | None] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = created_at()


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (one_of("role", MEMBERSHIP_ROLES, "role"),)

    org_id: Mapped[str] = fk("organizations.id", ondelete="CASCADE", primary_key=True)
    user_id: Mapped[str] = fk("users.id", ondelete="CASCADE", primary_key=True)
    role: Mapped[str] = mapped_column(String(20))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = created_at()


class Session(Base):
    """Server-side login session. `id` is sha256(token); the raw token lives only in the cookie."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = fk("users.id", ondelete="CASCADE")
    created_at: Mapped[datetime] = created_at()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PlatformOperator(Base):
    __tablename__ = "platform_operators"

    user_id: Mapped[str] = fk("users.id", ondelete="CASCADE", primary_key=True)
    created_at: Mapped[datetime] = created_at()
