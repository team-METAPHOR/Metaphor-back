import asyncio
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.hashing import lookup_hash
from app.core.ids import new_id
from app.models import AuditEvent, Session, User
from app.repositories import users as repo
from app.services.audit import record_audit
from app.services.limits import enforce_rate_limit, lock_key, since

SESSION_TTL = timedelta(days=14)
LOGIN_FAILED = "auth.login_failed"
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW = {"minutes": 15}
SIGNUP = "auth.signup"
SIGNUP_HOURLY_LIMIT = 5
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        actual = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p)
        )
    except ValueError:
        return False
    return hmac.compare_digest(actual.hex(), digest)


# Verified against when the email is unknown, so both paths cost one scrypt.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def signup(
    db: AsyncSession, email: str, password: str, display_name: str | None, client_ip: str
) -> User:
    """Create a user. 5 signups per client IP per hour, counted from audit_events."""
    email = email.strip().lower()
    ip_key = lookup_hash(client_ip)
    password_hash = await asyncio.to_thread(hash_password, password)
    async with db.begin():
        await lock_key(db, f"signup:{ip_key}")
        recent = (
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.action == SIGNUP,
                AuditEvent.target == ip_key,
                AuditEvent.created_at >= since(hours=1),
            )
        )
        await enforce_rate_limit(
            db,
            recent,
            limit=SIGNUP_HOURLY_LIMIT,
            code="SIGNUP_THROTTLED",
            message="가입 시도가 너무 많습니다. 잠시 후 다시 시도해 주세요.",
        )
        if await repo.get_by_email(db, email):
            raise ApiError(409, "EMAIL_TAKEN", "Email is already registered")
        user = User(
            id=new_id(), email=email, password_hash=password_hash, display_name=display_name
        )
        user = await repo.add_user(db, user)
        await record_audit(db, actor_id=user.id, org_id=None, action=SIGNUP, target=ip_key)
        return user


async def login(db: AsyncSession, email: str, password: str) -> tuple[User, str]:
    """Return the user and a fresh raw session token. Old sessions are revoked (no fixation).

    5 failures for one email within 15 minutes block that email, even with the right password.
    Failures are counted from audit_events (shared by every API process), keyed by
    lookup_hash(email) so the raw address is never stored.
    """
    email = email.strip().lower()
    email_hash = lookup_hash(email)
    async with db.begin():
        # Serialize logins per email until commit, so the count-then-record below is atomic.
        await db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(email_hash, 0))))
        failures = (
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.action == LOGIN_FAILED,
                AuditEvent.target == email_hash,
                AuditEvent.created_at >= since(**LOGIN_WINDOW),
            )
        )
        await enforce_rate_limit(
            db,
            failures,
            limit=LOGIN_MAX_FAILURES,
            code="LOGIN_THROTTLED",
            message="로그인 시도가 너무 많습니다. 잠시 후 다시 시도해 주세요.",
        )
        user = await repo.get_by_email(db, email)
        stored = user.password_hash if user else _DUMMY_HASH
        ok = await asyncio.to_thread(verify_password, password, stored)
        if user and ok and user.is_active:
            await repo.delete_user_sessions(db, user.id)
            token = secrets.token_urlsafe(32)
            expires_at = datetime.now(UTC) + SESSION_TTL
            await repo.add_session(
                db, Session(id=token_hash(token), user_id=user.id, expires_at=expires_at)
            )
            return user, token
        # Recorded in this transaction, which commits on block exit; raise only after that.
        await record_audit(db, actor_id=None, org_id=None, action=LOGIN_FAILED, target=email_hash)
    raise ApiError(401, "INVALID_CREDENTIALS", "Invalid email or password")


async def change_password(db: AsyncSession, user_id: str, current: str, new: str) -> None:
    """Verify the current password, store the new hash and revoke every session."""
    new_hash = await asyncio.to_thread(hash_password, new)
    async with db.begin():
        user = await db.get(User, user_id)
        if user is None or not await asyncio.to_thread(
            verify_password, current, user.password_hash
        ):
            raise ApiError(401, "INVALID_CREDENTIALS", "Invalid email or password")
        user.password_hash = new_hash
        await revoke_user_sessions(db, user_id)


async def logout(db: AsyncSession, token: str) -> None:
    async with db.begin():
        await repo.delete_session(db, token_hash(token))


async def user_for_token(db: AsyncSession, token: str) -> User | None:
    async with db.begin():
        return await repo.get_session_user(db, token_hash(token), datetime.now(UTC))


async def memberships(db: AsyncSession, user_id: str) -> list[tuple[str, str, str]]:
    async with db.begin():
        return await repo.list_memberships(db, user_id)


async def revoke_user_sessions(db: AsyncSession, user_id: str) -> None:
    """Delete every session of the user. Runs inside the caller's transaction."""
    await repo.delete_user_sessions(db, user_id)
