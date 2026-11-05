from typing import Annotated

from fastapi import APIRouter, Header, Response
from fastapi.responses import JSONResponse

from app.api.dependencies import CurrentUser, DbSession
from app.schemas.org import (
    MemberResponse,
    MemberRolePatch,
    OrgCreate,
    OrgDetail,
    OrgPatch,
    OrgResponse,
)
from app.services import orgs as service

router = APIRouter(tags=["orgs"])


@router.post("/orgs", status_code=201, response_model=OrgResponse)
async def create_org(
    body: OrgCreate,
    user: CurrentUser,
    db: DbSession,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    status, payload = await service.create_org(db, user.id, body.name, idempotency_key)
    return JSONResponse(payload, status_code=status)


@router.get("/orgs/{org_id}", response_model=OrgDetail)
async def get_org(org_id: str, user: CurrentUser, db: DbSession):
    return await service.get_org(db, user.id, org_id)


@router.patch("/orgs/{org_id}", response_model=OrgDetail)
async def rename_org(org_id: str, body: OrgPatch, user: CurrentUser, db: DbSession):
    return await service.rename_org(db, user.id, org_id, body.name)


@router.get("/orgs/{org_id}/members", response_model=list[MemberResponse])
async def list_members(org_id: str, user: CurrentUser, db: DbSession):
    return await service.list_members(db, user.id, org_id)


@router.patch("/orgs/{org_id}/members/{user_id}", response_model=MemberResponse)
async def change_member_role(
    org_id: str, user_id: str, body: MemberRolePatch, user: CurrentUser, db: DbSession
):
    return await service.change_member_role(db, user.id, org_id, user_id, body.role)


@router.delete("/orgs/{org_id}/members/{user_id}", status_code=204)
async def remove_member(org_id: str, user_id: str, user: CurrentUser, db: DbSession) -> Response:
    await service.remove_member(db, user.id, org_id, user_id)
    return Response(status_code=204)
