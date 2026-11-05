from fastapi import APIRouter, Response

from app.api.dependencies import CurrentUser, DbSession
from app.models import ProgramTestCondition
from app.schemas.test_condition import (
    TestConditionCreate,
    TestConditionList,
    TestConditionOut,
    TestConditionPatch,
)
from app.services import test_conditions as service

router = APIRouter(prefix="/programs/{program_id}/test-conditions", tags=["test-conditions"])


def _out(row: ProgramTestCondition) -> TestConditionOut:
    return TestConditionOut(
        condition_id=row.condition_id,
        label=row.label,
        browser_name=row.browser_name,
        width=row.width,
        height=row.height,
        response_delay_ms=row.response_delay_ms,
        test_context_id=row.test_context_id,
        record_video=row.record_video,
        reset_adapter=row.reset_adapter,
        has_session=row.session_secret_ref is not None,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("", response_model=TestConditionList)
async def list_conditions(program_id: str, user: CurrentUser, db: DbSession):
    rows = await service.list_conditions(db, user.id, program_id)
    return TestConditionList(items=[_out(row) for row in rows])


@router.post("", status_code=201, response_model=TestConditionOut)
async def create_condition(
    program_id: str, body: TestConditionCreate, user: CurrentUser, db: DbSession
):
    return _out(await service.create_condition(db, user.id, program_id, body))


@router.patch("/{condition_id}", response_model=TestConditionOut)
async def update_condition(
    program_id: str, condition_id: str, body: TestConditionPatch, user: CurrentUser, db: DbSession
):
    return _out(await service.update_condition(db, user.id, program_id, condition_id, body))


@router.delete("/{condition_id}", status_code=204)
async def delete_condition(program_id: str, condition_id: str, user: CurrentUser, db: DbSession):
    await service.delete_condition(db, user.id, program_id, condition_id)
    return Response(status_code=204)
