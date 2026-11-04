from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ProgramWidgetSettings


async def get(db: AsyncSession, program_id: str) -> ProgramWidgetSettings | None:
    return await db.get(ProgramWidgetSettings, program_id)


async def get_by_key(db: AsyncSession, key: str) -> ProgramWidgetSettings | None:
    return await db.scalar(
        select(ProgramWidgetSettings).where(ProgramWidgetSettings.public_project_key == key)
    )


async def add(db: AsyncSession, row: ProgramWidgetSettings) -> None:
    db.add(row)
    await db.flush()
