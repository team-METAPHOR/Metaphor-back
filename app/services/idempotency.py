from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.models import IdempotencyRecord

TTL = timedelta(hours=24)
MAX_KEY_LENGTH = 128


async def run_once(
    session: AsyncSession,
    *,
    user_id: str | None,
    scope: str,
    key: str | None,
    request_hash: str,
    fn: Callable[[], Awaitable[tuple[int, dict]]],
) -> tuple[int, dict]:
    """Run `fn` once per (user, scope, key); replay the stored response afterwards.

    Call inside the caller's transaction so the record commits together with `fn`'s writes.
    A transaction-scoped advisory lock serializes concurrent requests with the same key.
    """
    if key is None:
        return await fn()
    if not 1 <= len(key) <= MAX_KEY_LENGTH or not key.isprintable():
        raise ApiError(400, "IDEMPOTENCY_KEY_INVALID", "Idempotency-Key 형식이 올바르지 않습니다.")

    lock_key = f"{user_id or '-'}|{scope}|{key}"
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0))))
    match = (
        IdempotencyRecord.user_id.is_not_distinct_from(user_id),
        IdempotencyRecord.scope == scope,
        IdempotencyRecord.key == key,
    )
    record = await session.scalar(select(IdempotencyRecord).where(*match))
    now = datetime.now(UTC)
    if record is not None and record.expires_at <= now:
        await session.execute(delete(IdempotencyRecord).where(*match))
        record = None
    if record is not None:
        if record.request_hash != request_hash:
            raise ApiError(
                409,
                "IDEMPOTENCY_KEY_REUSED",
                "같은 Idempotency-Key로 다른 요청을 보낼 수 없습니다.",
            )
        return record.status_code, record.response

    status, body = await fn()
    session.add(
        IdempotencyRecord(
            user_id=user_id,
            scope=scope,
            key=key,
            request_hash=request_hash,
            status_code=status,
            response=body,
            expires_at=now + TTL,
        )
    )
    await session.flush()
    return status, body
