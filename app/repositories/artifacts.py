from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Artifact


async def get(db: AsyncSession, artifact_id: str) -> Artifact | None:
    return await db.get(Artifact, artifact_id)


async def list_for_job(db: AsyncSession, job_id: str, now: datetime) -> list[Artifact]:
    query = (
        select(Artifact)
        .where(Artifact.job_id == job_id, Artifact.expires_at > now)
        .order_by(Artifact.created_at, Artifact.id)
    )
    return list((await db.scalars(query)).all())
