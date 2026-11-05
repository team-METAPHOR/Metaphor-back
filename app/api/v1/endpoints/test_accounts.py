from fastapi import APIRouter, Response

from app.api.dependencies import CurrentUser, DbSession
from app.models import ProgramTestAccount
from app.schemas.test_account import TestAccountPut, TestAccountResponse
from app.services import test_accounts as service

router = APIRouter(prefix="/programs/{program_id}/test-account", tags=["test-account"])


def _response(found: tuple[ProgramTestAccount | None, bool]) -> TestAccountResponse:
    row, in_use = found
    if row is None:
        return TestAccountResponse(
            configured=False, login_id_masked=None, login_path=None, updated_at=None
        )
    return TestAccountResponse(
        configured=True,
        in_use=in_use,
        login_id_masked=row.login_id_masked,
        login_path=row.login_path,
        submit_path=row.submit_path,
        success_path=row.success_path,
        success_selector=row.success_selector,
        updated_at=row.updated_at,
    )


@router.get("", response_model=TestAccountResponse)
async def get_test_account(program_id: str, user: CurrentUser, db: DbSession):
    return _response(await service.get_account(db, user.id, program_id))


@router.put("", response_model=TestAccountResponse)
async def put_test_account(program_id: str, body: TestAccountPut, user: CurrentUser, db: DbSession):
    return _response(await service.put_account(db, user.id, program_id, body))


@router.delete("", status_code=204)
async def delete_test_account(program_id: str, user: CurrentUser, db: DbSession) -> Response:
    await service.delete_account(db, user.id, program_id)
    return Response(status_code=204)
