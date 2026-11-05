from sqlalchemy import delete, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ProgramTestAccount


async def get(
    db: AsyncSession, program_id: str, *, for_update: bool = False
) -> ProgramTestAccount | None:
    query = select(ProgramTestAccount).where(ProgramTestAccount.program_id == program_id)
    if for_update:
        query = query.with_for_update()
    return await db.scalar(query)


async def is_configured(db: AsyncSession, program_id: str) -> bool:
    return bool(
        await db.scalar(select(exists().where(ProgramTestAccount.program_id == program_id)))
    )


async def add(db: AsyncSession, row: ProgramTestAccount) -> None:
    db.add(row)
    await db.flush()


async def remove(db: AsyncSession, program_id: str) -> bool:
    result = await db.execute(
        delete(ProgramTestAccount).where(ProgramTestAccount.program_id == program_id)
    )
    return result.rowcount > 0
