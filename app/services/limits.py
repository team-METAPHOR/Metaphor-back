from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, func, select
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


async def lock_key(session: AsyncSession, key: str) -> None:
    """Transaction-scoped advisory lock so a count-then-insert on `key` is atomic."""
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0))))
