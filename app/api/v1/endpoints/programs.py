from fastapi import APIRouter

from app.api.dependencies import CurrentUser, DbSession
from app.models import OriginVerification, Program, ProgramScopeRevision
from app.schemas.program import (
    OriginVerificationResponse,
    ProgramCreate,
    ProgramList,
    ProgramPatch,
    ProgramResponse,
    ProgramSummary,
    ScopeResponse,
)
from app.services import origin_verification, test_accounts
from app.services import programs as service

router = APIRouter(prefix="/programs", tags=["programs"])


def _full(
    program: Program, revision: ProgramScopeRevision, has_test_account: bool = False
) -> ProgramResponse:
    return ProgramResponse(
        id=program.id,
        org_id=program.owner_org_id,
        name=program.name,
        status=program.status,
        external_ai_allowed=program.external_ai_allowed,
        daily_job_limit=program.daily_job_limit,
        retention_days=program.retention_days,
        auto_fresh_retries=program.auto_fresh_retries,
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
            has_session=revision.session_secret_ref is not None or has_test_account,
            has_test_account=has_test_account,
            credentials_blocked_by_http=service.credentials_blocked_by_http(
                revision, has_test_account
            ),
            reset_adapter=revision.reset_adapter,
            created_at=revision.created_at,
        ),
    )


@router.post("", status_code=201, response_model=ProgramResponse)
async def create_program(body: ProgramCreate, user: CurrentUser, db: DbSession):
    return _full(*await service.create_program(db, user.id, body))


@router.get("", response_model=ProgramList)
async def list_programs(user: CurrentUser, db: DbSession):
    rows = await service.list_programs(db, user.id)
    return ProgramList(
        items=[
            ProgramSummary(id=p.id, name=p.name, status=p.status, base_url=r.base_url, role=role)
            for p, r, role in rows
        ]
    )


@router.get("/{program_id}", response_model=ProgramResponse)
async def get_program(program_id: str, user: CurrentUser, db: DbSession):
    program, revision = await service.get_program(db, user.id, program_id)
    return _full(program, revision, await test_accounts.is_configured(db, program.id))


@router.patch("/{program_id}", response_model=ProgramResponse)
async def update_program(program_id: str, body: ProgramPatch, user: CurrentUser, db: DbSession):
    program, revision = await service.update_program(db, user.id, program_id, body)
    return _full(program, revision, await test_accounts.is_configured(db, program.id))


def _verification(row: OriginVerification) -> OriginVerificationResponse:
    return OriginVerificationResponse(
        origin=row.origin,
        token=row.token,
        file_url=origin_verification.file_url(row.origin),
        verified_at=row.verified_at,
        last_checked_at=row.last_checked_at,
    )


@router.get("/{program_id}/origin-verification", response_model=OriginVerificationResponse)
async def get_origin_verification(program_id: str, user: CurrentUser, db: DbSession):
    return _verification(await origin_verification.get_verification(db, user.id, program_id))


@router.post("/{program_id}/origin-verification/check", response_model=OriginVerificationResponse)
async def check_origin_verification(program_id: str, user: CurrentUser, db: DbSession):
    return _verification(await origin_verification.check_verification(db, user.id, program_id))
