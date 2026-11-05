from typing import Annotated

from fastapi import APIRouter, Header, Path, Query, Response
from fastapi.responses import JSONResponse

from app.api.dependencies import CurrentUser, DbSession
from app.schemas.org import (
    InviteAccept,
    InviteAccepted,
    InviteCreate,
    InviteCreated,
    InviteResponse,
    MemberResponse,
    MemberRolePatch,
    OrgCreate,
    OrgDetail,
    OrgPatch,
    OrgResponse,
)
from app.schemas.report import ReportDetail, ReportStats, ReportSummary
from app.services import invites, reports
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


@router.post("/orgs/{org_id}/invites", status_code=201, response_model=InviteCreated)
async def create_invite(org_id: str, body: InviteCreate, user: CurrentUser, db: DbSession):
    return await invites.create_invite(db, user.id, org_id, body.role, body.expires_in_days)


@router.get("/orgs/{org_id}/invites", response_model=list[InviteResponse])
async def list_invites(org_id: str, user: CurrentUser, db: DbSession):
    return await invites.list_invites(db, user.id, org_id)


@router.delete("/orgs/{org_id}/invites/{invite_id}", status_code=204)
async def revoke_invite(org_id: str, invite_id: str, user: CurrentUser, db: DbSession) -> Response:
    await invites.revoke_invite(db, user.id, org_id, invite_id)
    return Response(status_code=204)


@router.post("/invites/accept", response_model=InviteAccepted)
async def accept_invite(body: InviteAccept, user: CurrentUser, db: DbSession):
    return await invites.accept_invite(db, user.id, body.token)


@router.get("/orgs/{org_id}/reports/{number}", response_model=ReportDetail)
async def get_report_by_number(
    org_id: str, number: Annotated[int, Path(ge=1)], user: CurrentUser, db: DbSession
):
    """Report detail by the org's human number (BUG-<number>); members only, 404 otherwise."""
    return await reports.get_report_by_number(db, user.id, org_id, number)


@router.get("/orgs/{org_id}/report-summary", response_model=ReportSummary)
async def report_summary(org_id: str, user: CurrentUser, db: DbSession):
    """Dashboard cards: report counts by progress and the newest reports needing attention."""
    return await reports.report_summary(db, user.id, org_id)


@router.get("/orgs/{org_id}/report-stats", response_model=ReportStats)
async def report_stats(
    org_id: str, user: CurrentUser, db: DbSession, days: Annotated[int, Query()] = 7
):
    """Reproduction metrics over the last `days` days (clamped to 1..90)."""
    return await reports.report_stats(db, user.id, org_id, days)
