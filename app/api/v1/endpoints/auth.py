from fastapi import APIRouter, Request, Response

from app.api.dependencies import SESSION_COOKIE, CurrentUser, DbSession
from app.core.config import get_settings
from app.schemas.auth import (
    LoginRequest,
    MembershipResponse,
    MeResponse,
    SignupRequest,
    UserResponse,
)
from app.services import auth

router = APIRouter(prefix="/auth", tags=["auth"])


def _user(user) -> UserResponse:
    return UserResponse(id=user.id, email=user.email, display_name=user.display_name)


@router.post("/signup", status_code=201, response_model=UserResponse)
async def signup(body: SignupRequest, db: DbSession) -> UserResponse:
    return _user(await auth.signup(db, body.email, body.password, body.display_name))


@router.post("/login", response_model=UserResponse)
async def login(body: LoginRequest, db: DbSession, response: Response) -> UserResponse:
    user, token = await auth.login(db, body.email, body.password)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(auth.SESSION_TTL.total_seconds()),
        path="/api",
        httponly=True,
        samesite="lax",
        secure=not get_settings().debug,
    )
    return _user(user)


@router.post("/logout", status_code=204)
async def logout(request: Request, db: DbSession) -> Response:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await auth.logout(db, token)
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, path="/api")
    return response


@router.get("/me", response_model=MeResponse)
async def me(user: CurrentUser, db: DbSession) -> MeResponse:
    rows = await auth.memberships(db, user.id)
    return MeResponse(
        **_user(user).model_dump(),
        memberships=[MembershipResponse(org_id=o, org_name=n, role=r) for o, n, r in rows],
    )
