from sqlalchemy import delete, exists, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Membership, Organization, Program, ProgramInvite, ProgramScopeRevision


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


async def is_invited(db: AsyncSession, program_id: str, user_id: str) -> bool:
    return bool(
        await db.scalar(
            select(
                exists().where(
                    ProgramInvite.program_id == program_id, ProgramInvite.user_id == user_id
                )
            )
        )
    )


async def add_invite(db: AsyncSession, program_id: str, user_id: str) -> None:
    await db.execute(
        insert(ProgramInvite)
        .values(program_id=program_id, user_id=user_id)
        .on_conflict_do_nothing()
    )


async def delete_invite(db: AsyncSession, program_id: str, user_id: str) -> None:
    await db.execute(
        delete(ProgramInvite).where(
            ProgramInvite.program_id == program_id, ProgramInvite.user_id == user_id
        )
    )


async def list_visible(
    db: AsyncSession, user_id: str
) -> list[tuple[Program, ProgramScopeRevision, str | None]]:
    """Programs of the user's active orgs (any status), plus active open/invited programs."""
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
    invited = exists().where(
        ProgramInvite.program_id == Program.id, ProgramInvite.user_id == user_id
    )
    query = (
        select(Program, ProgramScopeRevision, role.label("role"))
        .join(
            ProgramScopeRevision,
            (ProgramScopeRevision.program_id == Program.id)
            & (ProgramScopeRevision.revision == Program.current_revision),
        )
        .where(
            or_(
                role.is_not(None),
                (Program.status == "active")
                & or_(Program.submission_mode == "open_authenticated", invited),
            )
        )
        .order_by(Program.created_at, Program.id)
    )
    # ponytail: unpaginated; add cursor paging when a user sees hundreds of programs.
    return [tuple(row) for row in await db.execute(query)]
