from fastapi import APIRouter, Response

from app.api.dependencies import CurrentUser, DbSession
from app.models import Program, ProgramScopeRevision
from app.schemas.program import (
    InviteCreate,
    InviteResponse,
    ProgramCreate,
    ProgramList,
    ProgramPatch,
    ProgramPublic,
    ProgramResponse,
    ProgramSummary,
    ScopeResponse,
)
from app.services import programs as service

router = APIRouter(prefix="/programs", tags=["programs"])


def _full(program: Program, revision: ProgramScopeRevision) -> ProgramResponse:
    return ProgramResponse(
        id=program.id,
        org_id=program.owner_org_id,
        name=program.name,
        status=program.status,
        submission_mode=program.submission_mode,
        external_ai_allowed=program.external_ai_allowed,
        daily_job_limit=program.daily_job_limit,
        retention_days=program.retention_days,
        current_revision=program.current_revision,
        created_at=program.created_at,
        updated_at=program.updated_at,
        scope=ScopeResponse(
            revision=revision.revision,
            base_url=revision.base_url,
            allowed_path_prefixes=revision.allowed_path_prefixes,
            excluded_path_prefixes=revision.excluded_path_prefixes,
            width=revision.width,
            height=revision.height,
            app_version=revision.app_version,
            approved_app_versions=revision.approved_app_versions,
            test_context_id=revision.test_context_id,
            mask_selectors=revision.mask_selectors,
            has_session=revision.session_secret_ref is not None,
            reset_adapter=revision.reset_adapter,
            created_at=revision.created_at,
        ),
    )


def _public(program: Program, revision: ProgramScopeRevision) -> dict:
    return {
        "id": program.id,
        "name": program.name,
        "status": program.status,
        "submission_mode": program.submission_mode,
        "base_url": revision.base_url,
    }


@router.post("", status_code=201, response_model=ProgramResponse)
async def create_program(body: ProgramCreate, user: CurrentUser, db: DbSession):
    return _full(*await service.create_program(db, user.id, body))


@router.get("", response_model=ProgramList)
async def list_programs(user: CurrentUser, db: DbSession):
    rows = await service.list_programs(db, user.id)
    return ProgramList(items=[ProgramSummary(**_public(p, r), role=role) for p, r, role in rows])


@router.get("/{program_id}", response_model=ProgramResponse | ProgramPublic)
async def get_program(program_id: str, user: CurrentUser, db: DbSession):
    program, revision, role = await service.get_program(db, user.id, program_id)
    return _full(program, revision) if role else ProgramPublic(**_public(program, revision))


@router.patch("/{program_id}", response_model=ProgramResponse)
async def update_program(program_id: str, body: ProgramPatch, user: CurrentUser, db: DbSession):
    return _full(*await service.update_program(db, user.id, program_id, body))


@router.post("/{program_id}/invites", status_code=201, response_model=InviteResponse)
async def invite(program_id: str, body: InviteCreate, user: CurrentUser, db: DbSession):
    invitee_id = await service.invite(db, user.id, program_id, body.email)
    return InviteResponse(program_id=program_id, user_id=invitee_id)


@router.delete("/{program_id}/invites/{user_id}", status_code=204)
async def uninvite(program_id: str, user_id: str, user: CurrentUser, db: DbSession):
    await service.uninvite(db, user.id, program_id, user_id)
    return Response(status_code=204)
