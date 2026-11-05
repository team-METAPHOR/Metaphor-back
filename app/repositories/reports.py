from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import ColumnElement, Select, and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import Membership, Organization, Report, User, ValidationJob
from app.models.validation_job import ACTIVE_JOB_STATUSES


async def get(db: AsyncSession, report_id: str) -> Report | None:
    return await db.get(Report, report_id)


async def get_by_number(db: AsyncSession, org_id: str, number: int) -> Report | None:
    return await db.scalar(select(Report).where(Report.org_id == org_id, Report.number == number))


async def fix_verified_ids(db: AsyncSession, report_ids: list[str]) -> set[str]:
    """Reports whose latest finished job is a retest that passed (completed, not_reproduced).
    Jobs of a report run one at a time, so creation order is finish order."""
    if not report_ids:
        return set()
    latest = (
        select(
            ValidationJob.report_id, ValidationJob.kind, ValidationJob.status, ValidationJob.verdict
        )
        .where(
            ValidationJob.report_id.in_(report_ids),
            ValidationJob.status.not_in(ACTIVE_JOB_STATUSES),
        )
        .distinct(ValidationJob.report_id)
        .order_by(ValidationJob.report_id, ValidationJob.created_at.desc(), ValidationJob.id.desc())
        .subquery()
    )
    query = select(latest.c.report_id).where(
        latest.c.kind == "retest",
        latest.c.status == "completed",
        latest.c.verdict == "not_reproduced",
    )
    return set((await db.scalars(query)).all())


def fix_verified_sql() -> ColumnElement:
    """SQL twin of `fix_verified_ids`, correlated to `Report`: the latest finished job is a
    passing retest. Uses its own alias, so it composes with a joined latest `ValidationJob`."""
    job = aliased(ValidationJob)
    passed = and_(job.kind == "retest", job.status == "completed", job.verdict == "not_reproduced")
    latest = (
        select(passed)
        .where(job.report_id == Report.id, job.status.not_in(ACTIVE_JOB_STATUSES))
        .order_by(job.created_at.desc(), job.id.desc())
        .limit(1)
        .correlate(Report)
        .scalar_subquery()
    )
    return func.coalesce(latest, False)


def count_by_author(author_id: str, since: datetime) -> Select:
    return (
        select(func.count())
        .select_from(Report)
        .where(Report.author_id == author_id, Report.created_at >= since)
    )


def member_org_ids(user_id: str):
    return (
        select(Membership.org_id)
        .join(Organization, Organization.id == Membership.org_id)
        .where(Membership.user_id == user_id, Membership.is_active, Organization.is_active)
    )


def assignee_active():
    """True when the report's assignee is an active user with an active membership in the
    report's org. Correlated to `Report`, so it is evaluated in the same query."""
    return (
        exists()
        .where(
            Membership.user_id == Report.assignee_id,
            Membership.org_id == Report.org_id,
            Membership.is_active,
        )
        .correlate(Report)
        & User.is_active
    )


async def get_assignee(db: AsyncSession, report_id: str) -> tuple[User, bool] | None:
    """The report's assignee and whether they are still an active member, in one query."""
    query = (
        select(User, assignee_active())
        .select_from(Report)
        .join(User, User.id == Report.assignee_id)
        .where(Report.id == report_id)
    )
    row = (await db.execute(query)).first()
    return (row[0], row[1]) if row else None


def _visible(query: Select, user_id: str, where: Sequence[ColumnElement]) -> Select:
    return query.outerjoin(ValidationJob, ValidationJob.id == Report.latest_job_id).where(
        Report.org_id.in_(member_org_ids(user_id)), *where
    )


def matches_text(q: str) -> ColumnElement:
    """Case-insensitive substring match on title or description; LIKE wildcards are literal."""
    pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    return or_(
        Report.title.ilike(pattern, escape="\\"), Report.description.ilike(pattern, escape="\\")
    )


def filters(
    *,
    source: str | None = None,
    program_id: str | None = None,
    assignee_id: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    q: str | None = None,
) -> list[ColumnElement]:
    """Plain-value list filters. `created_from` is inclusive, `created_to` exclusive."""
    where = []
    if source:
        where.append(Report.source == source)
    if program_id:
        where.append(Report.program_id == program_id)
    if assignee_id:
        where.append(Report.assignee_id == assignee_id)
    if created_from is not None:
        where.append(Report.created_at >= created_from)
    if created_to is not None:
        where.append(Report.created_at < created_to)
    if q:
        where.append(matches_text(q))
    return where


async def list_visible(
    db: AsyncSession,
    user_id: str,
    *,
    limit: int,
    cursor: tuple[datetime, str] | None = None,
    offset: int = 0,
    where: Sequence[ColumnElement] = (),
) -> list[tuple[Report, ValidationJob | None, User | None, bool | None]]:
    """Reports of the user's active orgs, newest first, with latest job, assignee and whether
    the assignee is still an active member of the report's org. `where` may reference
    `Report` and the latest `ValidationJob`. Returns up to `limit + 1` rows."""
    query = (
        _visible(select(Report, ValidationJob, User, assignee_active()), user_id, where)
        .outerjoin(User, User.id == Report.assignee_id)
        .order_by(Report.created_at.desc(), Report.id.desc())
        .limit(limit + 1)
        .offset(offset)
    )
    if cursor is not None:
        moment, last_id = cursor
        query = query.where(
            or_(Report.created_at < moment, and_(Report.created_at == moment, Report.id < last_id))
        )
    return [tuple(row) for row in (await db.execute(query)).all()]


async def count_visible(db: AsyncSession, user_id: str, where: Sequence[ColumnElement] = ()) -> int:
    query = _visible(select(func.count()).select_from(Report), user_id, where)
    return await db.scalar(query)


# --- org dashboard ---------------------------------------------------------


async def count_by(db: AsyncSession, org_id: str, expr: ColumnElement) -> dict[str, int]:
    """Org's report counts grouped by `expr` (may reference the latest `ValidationJob`)."""
    query = (
        select(expr, func.count())
        .select_from(Report)
        .outerjoin(ValidationJob, ValidationJob.id == Report.latest_job_id)
        .where(Report.org_id == org_id)
        .group_by(expr)
    )
    return {key: n for key, n in (await db.execute(query)).all()}


async def newest_matching(
    db: AsyncSession, org_id: str, condition: ColumnElement, limit: int
) -> list[tuple[Report, ValidationJob | None]]:
    query = (
        select(Report, ValidationJob)
        .outerjoin(ValidationJob, ValidationJob.id == Report.latest_job_id)
        .where(Report.org_id == org_id, condition)
        .order_by(Report.created_at.desc(), Report.id.desc())
        .limit(limit)
    )
    return [tuple(row) for row in (await db.execute(query)).all()]


# Finished for statistics: cancelled runs say nothing about reproduction.
STAT_STATUSES = ("completed", "inconclusive", "needs_info", "failed")


async def reproduction_stats(
    db: AsyncSession, org_id: str, since: datetime
) -> tuple[int, int, float | None]:
    """(finished initial jobs, of those whose report was reproduced by a finished initial or
    fresh job, average initial-job seconds from creation to finish) for initial jobs created
    since `since`."""
    reproducing = (
        select(1)
        .select_from(ValidationJob)
        .where(
            ValidationJob.report_id == Report.id,
            ValidationJob.kind.in_(("initial", "fresh")),
            ValidationJob.status.in_(STAT_STATUSES),
            ValidationJob.verdict == "reproduced",
        )
        .correlate(Report)
        .exists()
    )
    initial = ValidationJob.__table__.alias("initial")
    query = (
        select(
            func.count(),
            func.count().filter(reproducing),
            func.avg(func.extract("epoch", initial.c.finished_at - initial.c.created_at)),
        )
        .select_from(initial)
        .join(Report, Report.id == initial.c.report_id)
        .where(
            initial.c.org_id == org_id,
            initial.c.kind == "initial",
            initial.c.status.in_(STAT_STATUSES),
            initial.c.finished_at.is_not(None),
            initial.c.created_at >= since,
        )
    )
    total, reproduced, avg = (await db.execute(query)).one()
    return total, reproduced, None if avg is None else float(avg)


async def count_passed_retests(db: AsyncSession, org_id: str, since: datetime) -> int:
    query = (
        select(func.count())
        .select_from(ValidationJob)
        .where(
            ValidationJob.org_id == org_id,
            ValidationJob.kind == "retest",
            ValidationJob.status == "completed",
            ValidationJob.verdict == "not_reproduced",
            ValidationJob.created_at >= since,
        )
    )
    return await db.scalar(query)


def _url_path(url: ColumnElement) -> ColumnElement:
    """Path of an absolute URL: drop scheme://host, query and fragment; empty → "/"."""
    rest = func.regexp_replace(url, r"^[A-Za-z][A-Za-z0-9+.-]*://[^/?#]*", "")
    path = func.split_part(func.split_part(rest, "?", 1), "#", 1)
    return func.coalesce(func.nullif(path, ""), "/")


async def top_pages(
    db: AsyncSession, org_id: str, since: datetime, limit: int
) -> list[tuple[str, int]]:
    """Most reported page paths: the widget's reported_url, else the report's target_url."""
    url = func.coalesce(Report.client_context["reported_url"].astext, Report.target_url)
    path = _url_path(url).label("path")
    query = (
        select(path, func.count())
        .where(Report.org_id == org_id, Report.created_at >= since)
        .group_by(path)
        .order_by(func.count().desc(), path)
        .limit(limit)
    )
    return [tuple(row) for row in (await db.execute(query)).all()]
