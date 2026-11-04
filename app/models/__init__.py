"""ORM models. Import new model modules here so Alembic can discover them."""

from app.models.base import Base

__all__ = ["Base"]
