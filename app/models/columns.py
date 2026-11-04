"""Shared column helpers for domain models."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

ID_LENGTH = 32


def id_pk() -> Mapped[str]:
    return mapped_column(String(ID_LENGTH), primary_key=True)


def fk(
    target: str,
    *,
    nullable: bool = False,
    ondelete: str | None = None,
    primary_key: bool = False,
    index: bool = True,
) -> Mapped:
    return mapped_column(
        String(ID_LENGTH),
        ForeignKey(target, ondelete=ondelete),
        nullable=nullable,
        primary_key=primary_key,
        index=index and not primary_key,
    )


def created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def updated_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


def one_of(column: str, values: tuple[str, ...], name: str) -> CheckConstraint:
    quoted = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IN ({quoted})", name=name)
