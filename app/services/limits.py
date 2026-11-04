from datetime import UTC, datetime, timedelta

from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError


def since(**delta: float) -> datetime:
    """`since(hours=1)` → now minus that window, for count queries."""
    return datetime.now(UTC) - timedelta(**delta)


async def enforce_rate_limit(
    session: AsyncSession,
    count_stmt: Select,
    *,
    limit: int,
    code: str,
    message: str = "요청 한도를 초과했습니다. 잠시 후 다시 시도해 주세요.",
) -> None:
    """Raise 429 when `count_stmt` (a SELECT count(...)) is already at `limit`.

    ponytail: counts existing rows instead of a token bucket; fine for per-hour/day limits.
    """
    if (await session.scalar(count_stmt) or 0) >= limit:
        raise ApiError(429, code, message)
