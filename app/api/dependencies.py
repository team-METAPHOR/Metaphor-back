from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import ApiError
from app.db.session import get_db_session
from app.models import User
from app.services import auth

DbSession = Annotated[AsyncSession, Depends(get_db_session)]

SESSION_COOKIE = "sid"
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


async def current_user(request: Request, db: DbSession) -> User:
    token = request.cookies.get(SESSION_COOKIE)
    user = await auth.user_for_token(db, token) if token else None
    if user is None:
        raise ApiError(401, "UNAUTHENTICATED", "Login required")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


async def require_csrf(request: Request) -> None:
    """Cookie-auth routers only. A missing Origin on an unsafe method is rejected too."""
    if request.method in _UNSAFE_METHODS:
        if request.headers.get("origin") not in get_settings().allowed_origins:
            raise ApiError(403, "CSRF_REJECTED", "Origin not allowed")
