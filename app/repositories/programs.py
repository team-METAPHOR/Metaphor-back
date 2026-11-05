from sqlalchemy import exists, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Membership, Organization, OriginVerification, Program, ProgramScopeRevision


async def add(db: AsyncSession, *rows) -> None:
    db.add_all(rows)
    await db.flush()


async def get(db: AsyncSession, program_id: str, *, for_update: bool = False) -> Program | None:
    query = select(Program).where(Program.id == program_id)
    if for_update:
        query = query.with_for_update()
    return await db.scalar(query)


async def get_revision(db: AsyncSession, program: Program) -> ProgramScopeRevision:
    return await db.get(ProgramScopeRevision, (program.id, program.current_revision))


async def list_visible(
    db: AsyncSession, user_id: str
) -> list[tuple[Program, ProgramScopeRevision, str]]:
    """Programs (any status) of the user's active orgs, with the user's role."""
    role = (
        select(Membership.role)
        .join(Organization, Organization.id == Membership.org_id)
        .where(
            Membership.org_id == Program.owner_org_id,
            Membership.user_id == user_id,
            Membership.is_active,
            Organization.is_active,
        )
        .scalar_subquery()
    )
    query = (
        select(Program, ProgramScopeRevision, role.label("role"))
        .join(
            ProgramScopeRevision,
            (ProgramScopeRevision.program_id == Program.id)
            & (ProgramScopeRevision.revision == Program.current_revision),
        )
        .where(role.is_not(None))
        .order_by(Program.created_at, Program.id)
    )
    # ponytail: unpaginated; add cursor paging when a user sees hundreds of programs.
    return [tuple(row) for row in await db.execute(query)]


async def ensure_verification(
    db: AsyncSession, program_id: str, origin: str, token: str, *, for_update: bool = False
) -> OriginVerification:
    """Get the (program, origin) verification row, inserting it with `token` if missing.

    ON CONFLICT keeps concurrent first reads from failing; the first token wins.
    """
    await db.execute(
        insert(OriginVerification)
        .values(program_id=program_id, origin=origin, token=token)
        .on_conflict_do_nothing()
    )
    query = select(OriginVerification).where(
        OriginVerification.program_id == program_id, OriginVerification.origin == origin
    )
    if for_update:
        query = query.with_for_update()
    return await db.scalar(query.execution_options(populate_existing=True))


async def is_origin_verified(db: AsyncSession, program_id: str, origin: str) -> bool:
    return await db.scalar(
        select(
            exists().where(
                OriginVerification.program_id == program_id,
                OriginVerification.origin == origin,
                OriginVerification.verified_at.is_not(None),
            )
        )
    )
