from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Artifact,
    AuditEvent,
    JobEvent,
    QaResult,
    Report,
    ReportEvalTag,
    ValidationJob,
    ValidationJobCondition,
)


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


async def tags_for(db: AsyncSession, report_ids: list[str]) -> dict[str, ReportEvalTag]:
    rows = await db.scalars(select(ReportEvalTag).where(ReportEvalTag.report_id.in_(report_ids)))
    return {tag.report_id: tag for tag in rows}


async def jobs_for(db: AsyncSession, report_ids: list[str]) -> list[ValidationJob]:
    rows = await db.scalars(
        select(ValidationJob)
        .where(ValidationJob.report_id.in_(report_ids))
        .order_by(ValidationJob.created_at, ValidationJob.id)
    )
    return list(rows)


async def event_bounds(
    db: AsyncSession, job_ids: list[str], terminal: tuple[str, ...]
) -> dict[str, tuple[datetime | None, datetime | None]]:
    """(first engine event, the job's own engine terminal event) per job."""
    rows = await db.execute(
        select(
            JobEvent.job_id,
            func.min(JobEvent.created_at),
            func.max(JobEvent.created_at).filter(
                JobEvent.engine_source == "", JobEvent.kind.in_(terminal)
            ),
        )
        .where(JobEvent.job_id.in_(job_ids))
        .group_by(JobEvent.job_id)
    )
    return {job_id: (first, end) for job_id, first, end in rows}


async def result_parts(db: AsyncSession, job_ids: list[str]) -> dict[str, dict]:
    """Only the parts of each stored engine result the evaluation needs (not observations)."""
    report = QaResult.raw["report"]
    rows = await db.execute(
        select(
            QaResult.job_id,
            report["model_calls"],
            report["timings"],
            report["plan_review"],
            report["run"]["evidence"],
            report["elapsed_seconds"],
        ).where(QaResult.job_id.in_(job_ids))
    )
    return {
        job_id: {
            "model_calls": calls,
            "timings": timings,
            "plan_review": review,
            "evidence": evidence,
            "elapsed_seconds": elapsed,
        }
        for job_id, calls, timings, review, evidence, elapsed in rows
    }


async def conditions_for(db: AsyncSession, job_ids: list[str]) -> list[ValidationJobCondition]:
    rows = await db.scalars(
        select(ValidationJobCondition)
        .where(ValidationJobCondition.job_id.in_(job_ids))
        .order_by(ValidationJobCondition.job_id, ValidationJobCondition.ordinal)
    )
    return list(rows)


async def jobs_with_report_md(db: AsyncSession, job_ids: list[str]) -> set[str]:
    rows = await db.scalars(
        select(Artifact.job_id)
        .where(Artifact.job_id.in_(job_ids), Artifact.kind == "report_md")
        .distinct()
    )
    return set(rows)


async def automatic_job_ids(db: AsyncSession, job_ids: list[str], action: str) -> set[str]:
    rows = await db.scalars(
        select(AuditEvent.target).where(AuditEvent.action == action, AuditEvent.target.in_(job_ids))
    )
    return set(rows)


async def run_reports(
    db: AsyncSession, org_id: str, eval_run_id: str, limit: int
) -> list[tuple[Report, ReportEvalTag]]:
    rows = await db.execute(
        select(Report, ReportEvalTag)
        .join(ReportEvalTag, ReportEvalTag.report_id == Report.id)
        .where(ReportEvalTag.org_id == org_id, ReportEvalTag.eval_run_id == eval_run_id)
        .order_by(ReportEvalTag.case_id, ReportEvalTag.repeat_id)
        .limit(limit)
    )
    return [(report, tag) for report, tag in rows]
