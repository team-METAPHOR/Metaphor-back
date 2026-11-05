"""Evaluation bookkeeping (issue #105). Tags are evaluator-only metadata: nothing here is ever
read by the worker or copied into report text, engine input or report views."""

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.models import ReportEvalTag
from app.repositories import evaluation as repo
from app.schemas.evaluation import EvalTag, EvalTagUpdate
from app.services.access import WRITE_ROLES, org_role
from app.services.audit import record_audit
from app.services.reports import load_visible_report


def _slot_taken() -> ApiError:
    return ApiError(
        409, "EVAL_SLOT_TAKEN", "같은 평가 실행·사례·반복 번호가 이미 다른 제보에 연결돼 있습니다."
    )


def _out(tag: ReportEvalTag) -> EvalTag:
    return EvalTag(
        report_id=tag.report_id,
        eval_run_id=tag.eval_run_id,
        case_id=tag.case_id,
        repeat_id=tag.repeat_id,
        tagged_at=tag.updated_at,
    )


async def get_eval_tag(db: AsyncSession, user_id: str, report_id: str) -> EvalTag:
    async with db.begin():
        await load_visible_report(db, user_id, report_id)
        tag = await repo.get_tag(db, report_id)
        if tag is None:
            raise ApiError(404, "NOT_FOUND", "평가 태그가 없습니다.")
        return _out(tag)


async def set_eval_tag(
    db: AsyncSession, user_id: str, report_id: str, body: EvalTagUpdate
) -> EvalTag:
    """Attach (or replace) the report's evaluation slot. Developer/admin of the owner org."""
    values = body.model_dump()
    try:
        async with db.begin():
            report = await load_visible_report(db, user_id, report_id)
            if await org_role(db, user_id, report.org_id) not in WRITE_ROLES:
                raise ApiError(403, "FORBIDDEN", "평가 태그를 바꿀 권한이 없습니다.")
            owner = await repo.slot_owner(db, report.org_id, **values)
            if owner not in (None, report.id):
                raise _slot_taken()
            tag = await repo.get_tag(db, report.id)
            if tag is None:
                tag = ReportEvalTag(report_id=report.id, org_id=report.org_id)
                db.add(tag)
            for key, value in values.items():
                setattr(tag, key, value)
            tag.tagged_by = user_id
            await db.flush()
            await record_audit(
                db,
                actor_id=user_id,
                org_id=report.org_id,
                action="report.eval_tag",
                target=report.id,
                detail=values,
            )
            await db.refresh(tag)
            return _out(tag)
    except IntegrityError:
        # A concurrent PUT took the slot between the check and the insert.
        raise _slot_taken() from None
