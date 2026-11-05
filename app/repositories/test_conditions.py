from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ProgramTestCondition


async def list_for_program(db: AsyncSession, program_id: str) -> list[ProgramTestCondition]:
    query = (
        select(ProgramTestCondition)
        .where(ProgramTestCondition.program_id == program_id)
        .order_by(ProgramTestCondition.created_at, ProgramTestCondition.condition_id)
    )
    return list((await db.scalars(query)).all())


async def get(db: AsyncSession, program_id: str, condition_id: str) -> ProgramTestCondition | None:
    return await db.get(ProgramTestCondition, (program_id, condition_id))


async def count(db: AsyncSession, program_id: str) -> int:
    return await db.scalar(
        select(func.count())
        .select_from(ProgramTestCondition)
        .where(ProgramTestCondition.program_id == program_id)
    )


async def add(db: AsyncSession, row: ProgramTestCondition) -> None:
    db.add(row)
    await db.flush()


async def delete(db: AsyncSession, row: ProgramTestCondition) -> None:
    await db.delete(row)
    await db.flush()


async def unbind_sessions(db: AsyncSession, program_id: str) -> None:
    await db.execute(
        update(ProgramTestCondition)
        .where(ProgramTestCondition.program_id == program_id)
        .values(session_secret_ref=None)
    )
