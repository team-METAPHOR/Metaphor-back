"""Per-program test condition registry (admin CRUD) and the job snapshot of a selection."""

from core_engine import BrowserOptions, TestCondition
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.models import ProgramTestCondition
from app.models.test_condition import (
    DEFAULT_RUN_CONDITIONS,
    MAX_REGISTERED_CONDITIONS,
)
from app.repositories import programs as programs_repo
from app.repositories import test_conditions as repo
from app.schemas.test_condition import TestConditionCreate, TestConditionPatch
from app.services.access import ADMIN_ROLES, org_role, require_org_role
from app.services.audit import record_audit
from app.services.programs import ALLOWED_RESET_ADAPTERS


def _not_found() -> ApiError:
    return ApiError(404, "NOT_FOUND", "Not found")


def engine_condition(row: ProgramTestCondition, *, session_headers=None) -> TestCondition:
    """The engine's own type, so registration applies the rules the worker will apply.
    Without real headers a placeholder stands in for the operator-bound session."""
    if session_headers is None:
        session_headers = {"Cookie": "placeholder"} if row.session_secret_ref else {}
    return TestCondition(
        id=row.condition_id,
        label=row.label,
        options=BrowserOptions(
            width=row.width,
            height=row.height,
            test_context_id=row.test_context_id,
            browser_name=row.browser_name,
            record_video=row.record_video,
            response_delay_ms=row.response_delay_ms,
            session_headers=session_headers,
        ),
    )


def _validated(row: ProgramTestCondition) -> ProgramTestCondition:
    if row.reset_adapter not in ALLOWED_RESET_ADAPTERS:
        raise ApiError(422, "RESET_ADAPTER_UNKNOWN", "지원하지 않는 초기화 방식입니다.")
    try:
        engine_condition(row)
    except ValueError as error:
        raise ApiError(
            422,
            "INVALID_CONDITION",
            "테스트 조건 설정이 올바르지 않습니다.",
            {"reason": str(error)},
        ) from None
    return row


def snapshot_entry(row: ProgramTestCondition) -> dict:
    """Frozen into a job's scope snapshot. Secret *reference* only, as for the program."""
    return {
        "condition_id": row.condition_id,
        "label": row.label,
        "browser_name": row.browser_name,
        "width": row.width,
        "height": row.height,
        "response_delay_ms": row.response_delay_ms,
        "test_context_id": row.test_context_id,
        "record_video": row.record_video,
        "reset_adapter": row.reset_adapter,
        "session_secret_ref": row.session_secret_ref,
    }


async def _program(db: AsyncSession, user_id: str, program_id: str, *, admin: bool):
    program = await programs_repo.get(db, program_id, for_update=admin)
    if program is None:
        raise _not_found()
    if admin:
        await require_org_role(db, user_id, program.owner_org_id, *ADMIN_ROLES)
    elif await org_role(db, user_id, program.owner_org_id) is None:
        raise _not_found()
    return program


async def list_conditions(db: AsyncSession, user_id: str, program_id: str):
    async with db.begin():
        await _program(db, user_id, program_id, admin=False)
        return await repo.list_for_program(db, program_id)


async def create_condition(
    db: AsyncSession, user_id: str, program_id: str, body: TestConditionCreate
):
    async with db.begin():
        # Program row lock serializes registrations, so the 16-row limit is exact.
        program = await _program(db, user_id, program_id, admin=True)
        if await repo.get(db, program.id, body.condition_id) is not None:
            raise ApiError(409, "CONDITION_EXISTS", "같은 ID의 테스트 조건이 이미 있습니다.")
        if await repo.count(db, program.id) >= MAX_REGISTERED_CONDITIONS:
            raise ApiError(
                409, "CONDITION_LIMIT_EXCEEDED", "테스트 조건은 최대 16개까지 등록할 수 있습니다."
            )
        row = _validated(
            ProgramTestCondition(
                program_id=program.id,
                session_secret_ref=None,
                created_by=user_id,
                **body.model_dump(),
            )
        )
        await repo.add(db, row)
        await record_audit(
            db,
            actor_id=user_id,
            org_id=program.owner_org_id,
            action="program.condition.create",
            target=program.id,
            detail={"condition_id": row.condition_id},
        )
        await db.refresh(row)
        return row


async def update_condition(
    db: AsyncSession, user_id: str, program_id: str, condition_id: str, body: TestConditionPatch
):
    changes = body.model_dump(exclude_unset=True)
    async with db.begin():
        program = await _program(db, user_id, program_id, admin=True)
        row = await repo.get(db, program.id, condition_id)
        if row is None:
            raise _not_found()
        for field, value in changes.items():
            if value is None:
                raise ApiError(422, "VALIDATION_FAILED", f"{field} cannot be null")
        changed = sorted(f for f, v in changes.items() if getattr(row, f) != v)
        if not changed:
            return row
        if "test_context_id" in changed:
            # A session is bound to one synthetic account/context: re-binding is the operator's.
            row.session_secret_ref = None
        for field in changed:
            setattr(row, field, changes[field])
        _validated(row)
        await db.flush()
        await record_audit(
            db,
            actor_id=user_id,
            org_id=program.owner_org_id,
            action="program.condition.update",
            target=program.id,
            detail={"condition_id": row.condition_id, "fields": changed},
        )
        await db.refresh(row)
        return row


async def delete_condition(db: AsyncSession, user_id: str, program_id: str, condition_id: str):
    async with db.begin():
        program = await _program(db, user_id, program_id, admin=True)
        row = await repo.get(db, program.id, condition_id)
        if row is None:
            raise _not_found()
        await repo.delete(db, row)
        await record_audit(
            db,
            actor_id=user_id,
            org_id=program.owner_org_id,
            action="program.condition.delete",
            target=program.id,
            detail={"condition_id": condition_id},
        )


async def selection_snapshot(
    db: AsyncSession,
    program_id: str,
    condition_ids: list[str] | None,
    auto_select: bool,
    max_conditions: int | None,
) -> dict:
    """Snapshot keys for a condition-matrix job: the chosen registered conditions, frozen.

    Ids are looked up in this program's registry only, so another program's or org's ids are
    simply unknown (422). `max_conditions` defaults to the engine default (4) for auto
    selection and to the number of ids otherwise."""
    rows = {row.condition_id: row for row in await repo.list_for_program(db, program_id)}
    if condition_ids:
        unknown = [cid for cid in condition_ids if cid not in rows]
        if unknown:
            raise ApiError(
                422,
                "CONDITION_UNKNOWN",
                "등록되지 않은 테스트 조건입니다.",
                {"condition_ids": unknown},
            )
        chosen = [rows[cid] for cid in condition_ids]
    else:
        chosen = list(rows.values())
    if not chosen:
        raise ApiError(422, "NO_TEST_CONDITIONS", "등록된 테스트 조건이 없습니다.")
    if auto_select:
        limit = min(max_conditions or DEFAULT_RUN_CONDITIONS, len(chosen))
    else:
        limit = min(max_conditions or len(chosen), len(chosen))
    return {
        "conditions": [snapshot_entry(row) for row in chosen],
        "condition_select": auto_select,
        "max_conditions": limit,
    }
