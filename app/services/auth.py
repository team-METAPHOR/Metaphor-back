import asyncio
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.hashing import CLIENT_IP, LOGIN_EMAIL, LOGIN_IP_EMAIL, lookup_hash
from app.core.ids import new_id
from app.models import AuditEvent, Session, User
from app.repositories import users as repo
from app.services.audit import count_actions, record_audit
from app.services.limits import enforce_rate_limit, lock_key, since

SESSION_TTL = timedelta(days=14)
LOGIN_FAILED = "auth.login_failed"
LOGIN_MAX_FAILURES = 5
LOGIN_ACCOUNT_MAX_FAILURES = 50
LOGIN_SUCCEEDED = "auth.login_succeeded"
LOGIN_WINDOW = {"minutes": 15}
PASSWORD_CHANGE = "auth.password_change"
PASSWORD_CHANGE_FAILED = "auth.password_change_failed"
PASSWORD_CHANGE_MAX_FAILURES = 5
PASSWORD_CHANGE_WINDOW = {"hours": 1}
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
    ip_key = lookup_hash(CLIENT_IP, client_ip)
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


def _failures(key: str, cleared_by: str) -> Select:
    """LOGIN_FAILED rows for `key` in the window, after the last success that clears them."""
    last_success = (
        select(func.max(AuditEvent.created_at))
        .where(AuditEvent.action == LOGIN_SUCCEEDED, AuditEvent.target == cleared_by)
        .scalar_subquery()
    )
    return (
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.action == LOGIN_FAILED,
            AuditEvent.target == key,
            AuditEvent.created_at >= since(**LOGIN_WINDOW),
            AuditEvent.created_at > func.coalesce(last_success, datetime.min.replace(tzinfo=UTC)),
        )
    )


async def login(db: AsyncSession, email: str, password: str, client_ip: str) -> tuple[User, str]:
    """Return the user and a fresh raw session token. Old sessions are revoked (no fixation).

    Two failure counters, both from audit_events and keyed by HMAC so no raw PII is stored:
    - client IP + email: 5 failures in 15 minutes block that pair (brute force from one IP).
    - email alone: 50 failures in 15 minutes block the account (distributed guessing). It is
      high so a handful of failures from other IPs never lock out the right password.
    A success clears both counters. Limits apply to unknown emails too, so the responses do
    not reveal which accounts exist.
    """
    email = email.strip().lower()
    account_key = lookup_hash(LOGIN_EMAIL, email)
    pair_key = lookup_hash(LOGIN_IP_EMAIL, f"{client_ip}\n{email}")
    throttled = {
        "code": "LOGIN_THROTTLED",
        "message": "로그인 시도가 너무 많습니다. 잠시 후 다시 시도해 주세요.",
    }
    async with db.begin():
        # Serialize logins per email until commit, so the count-then-record below is atomic.
        await lock_key(db, f"login:{account_key}")
        await enforce_rate_limit(
            db, _failures(pair_key, pair_key), limit=LOGIN_MAX_FAILURES, **throttled
        )
        await enforce_rate_limit(
            db,
            _failures(account_key, account_key),
            limit=LOGIN_ACCOUNT_MAX_FAILURES,
            **throttled,
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
            for key in (pair_key, account_key):
                await record_audit(
                    db, actor_id=user.id, org_id=None, action=LOGIN_SUCCEEDED, target=key
                )
            return user, token
        # Recorded in this transaction, which commits on block exit; raise only after that.
        for key in (pair_key, account_key):
            await record_audit(db, actor_id=None, org_id=None, action=LOGIN_FAILED, target=key)
    raise ApiError(401, "INVALID_CREDENTIALS", "Invalid email or password")


async def change_password(db: AsyncSession, user_id: str, current: str, new: str) -> None:
    """Verify the current password, store the new hash and revoke every session.

    5 wrong current passwords per user per hour block further attempts (429), counted from
    audit_events under a per-user advisory lock so the count-then-record is atomic.
    """
    new_hash = await asyncio.to_thread(hash_password, new)
    async with db.begin():
        await lock_key(db, f"password_change:{user_id}")
        await enforce_rate_limit(
            db,
            count_actions(user_id, (PASSWORD_CHANGE_FAILED,), since(**PASSWORD_CHANGE_WINDOW)),
            limit=PASSWORD_CHANGE_MAX_FAILURES,
            code="PASSWORD_CHANGE_THROTTLED",
            message="비밀번호 변경 시도가 너무 많습니다. 잠시 후 다시 시도해 주세요.",
        )
        user = await db.get(User, user_id)
        if user is not None and await asyncio.to_thread(
            verify_password, current, user.password_hash
        ):
            user.password_hash = new_hash
            await revoke_user_sessions(db, user_id)
            await record_audit(db, actor_id=user_id, org_id=None, action=PASSWORD_CHANGE)
            return
        # Recorded in this transaction, which commits on block exit; raise only after that.
        await record_audit(db, actor_id=user_id, org_id=None, action=PASSWORD_CHANGE_FAILED)
    raise ApiError(401, "INVALID_CREDENTIALS", "Invalid email or password")


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
