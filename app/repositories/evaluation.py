from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ReportEvalTag


async def get_tag(db: AsyncSession, report_id: str) -> ReportEvalTag | None:
    return await db.get(ReportEvalTag, report_id)


async def slot_owner(
    db: AsyncSession, org_id: str, eval_run_id: str, case_id: str, repeat_id: str
) -> str | None:
    """Report id already holding this slot in the org, if any."""
    return await db.scalar(
        select(ReportEvalTag.report_id).where(
            ReportEvalTag.org_id == org_id,
            ReportEvalTag.eval_run_id == eval_run_id,
            ReportEvalTag.case_id == case_id,
            ReportEvalTag.repeat_id == repeat_id,
        )
    )
