"""Origin ownership verification: the owner publishes a token at a fixed well-known path.

Jobs only run against verified origins (see `create_job`), so nobody can register a site they
do not control and have our workers drive it. The check is an outbound request from the API and
is built to not be an SSRF primitive:

- exactly one GET of `<origin>/.well-known/metaphor-verify.txt`, http(s) only, no redirects;
- the hostname is resolved once and *every* answer is checked with the engine's own
  `RunPolicy.resolve_target` (private/loopback/reserved refused unless the operator approved the
  address via `allow_private_targets` + `private_cidrs`; link-local/multicast always refused);
- the socket connects to that checked IP (no second lookup, so no DNS rebinding between check
  and connect), while the Host header and TLS SNI/certificate check keep the hostname;
- 3 s timeout, at most 1 KB of body read, no cookies or credentials sent.
"""

import asyncio
import http.client
import secrets
import socket
from datetime import UTC, datetime
from urllib.parse import urlsplit

from core_engine import PolicyError, RunPolicy
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.models import AuditEvent, OriginVerification, Program, ProgramScopeRevision
from app.repositories import programs as repo
from app.services.access import require_org_role
from app.services.audit import record_audit
from app.services.limits import enforce_rate_limit, lock_key, since

VERIFY_PATH = "/.well-known/metaphor-verify.txt"
FETCH_TIMEOUT = 3.0
MAX_BODY = 1024
CHECKS_PER_MINUTE = 5
CHECK_ACTION = "program.origin_check"
VERIFIED_ACTION = "program.origin_verified"
_BLOCKED = ("PRIVATE_DESTINATION_BLOCKED", "DESTINATION_BLOCKED")
# Looked up at call time so tests can stand in a fake DNS.
resolver = socket.getaddrinfo


def origin_of(base_url: str) -> str:
    """`https://host[:port]` of a revision base_url (stored canonical, ending in "/")."""
    return base_url.rstrip("/")


def file_url(origin: str) -> str:
    return origin + VERIFY_PATH


class _Failed(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _failed(reason: str) -> ApiError:
    return ApiError(
        422, "ORIGIN_VERIFY_FAILED", "도메인 소유 확인에 실패했습니다.", {"reason": reason}
    )


def _policy(revision: ProgramScopeRevision) -> RunPolicy:
    """The revision's origin and private-target approval, scoped to the verification file."""
    return RunPolicy(
        program_id=revision.program_id,
        scope_revision=revision.revision,
        base_url=revision.base_url,
        allowed_path_prefixes=(VERIFY_PATH,),
        allow_private_targets=revision.allow_private_targets,
        private_cidrs=tuple(revision.private_cidrs),
    )


def fetch_file(policy: RunPolicy) -> bytes:
    """GET the verification file (blocking). Returns at most MAX_BODY + 1 bytes."""
    try:
        target = policy.resolve_target(policy.base_url.rstrip("/") + VERIFY_PATH, resolver=resolver)
    except PolicyError as error:
        raise _Failed("PRIVATE_ADDRESS" if error.code in _BLOCKED else "UNREACHABLE") from None
    https = urlsplit(target.url).scheme == "https"
    connection_class = http.client.HTTPSConnection if https else http.client.HTTPConnection
    conn = connection_class(target.hostname, target.port, timeout=FETCH_TIMEOUT)

    # Pin the TCP connection to the address resolve_target checked. http.client builds the Host
    # header and (for https) the SNI/cert hostname from `target.hostname`, so only the socket
    # destination changes. ponytail: `_create_connection` is a private http.client attribute
    # (stable since 3.x, used only by connect()); swap for a socket-level adapter if it moves.
    def pinned(_address, timeout, source_address=None):
        return socket.create_connection((target.address, target.port), timeout, source_address)

    conn._create_connection = pinned
    try:
        conn.request("GET", VERIFY_PATH, headers={"User-Agent": "Metaphor-Verify/1"})
        response = conn.getresponse()
        if 300 <= response.status < 400:
            raise _Failed("REDIRECT")
        if response.status != 200:
            raise _Failed("NOT_FOUND")
        return response.read(MAX_BODY + 1)
    except (OSError, http.client.HTTPException):  # includes timeouts and TLS errors
        raise _Failed("UNREACHABLE") from None
    finally:
        conn.close()


async def _check_reason(policy: RunPolicy, token: str) -> str | None:
    """None when the file holds exactly `token` (surrounding whitespace ignored)."""
    try:
        # Hard 3 s cap for the caller, DNS included. ponytail: on timeout the worker thread is
        # left to finish on its own socket timeout (also 3 s per operation); getaddrinfo itself
        # has no timeout, so a hung resolver can hold one thread-pool slot longer.
        body = await asyncio.wait_for(asyncio.to_thread(fetch_file, policy), FETCH_TIMEOUT)
    except _Failed as failure:
        return failure.reason
    except TimeoutError:
        return "UNREACHABLE"
    try:
        matches = len(body) <= MAX_BODY and body.decode("utf-8").strip() == token
    except UnicodeDecodeError:
        matches = False
    return None if matches else "MISMATCH"


async def _admin_program(db: AsyncSession, user_id: str, program_id: str) -> Program:
    program = await repo.get(db, program_id)
    if program is None:
        raise ApiError(404, "NOT_FOUND", "Not found")
    await require_org_role(db, user_id, program.owner_org_id, "admin")
    return program


def _new_token() -> str:
    return secrets.token_urlsafe(32)  # 43 chars


async def get_verification(db: AsyncSession, user_id: str, program_id: str) -> OriginVerification:
    """The current revision's origin record, created (unverified, new token) on first read."""
    async with db.begin():
        program = await _admin_program(db, user_id, program_id)
        revision = await repo.get_revision(db, program)
        return await repo.ensure_verification(
            db, program.id, origin_of(revision.base_url), _new_token()
        )


def _recent_checks(program_id: str):
    return (
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.action == CHECK_ACTION,
            AuditEvent.target == program_id,
            AuditEvent.created_at >= since(minutes=1),
        )
    )


async def check_verification(db: AsyncSession, user_id: str, program_id: str) -> OriginVerification:
    """Fetch the file and mark the origin verified, or 422 ORIGIN_VERIFY_FAILED {reason}.

    The attempt is committed (and counted for the per-program limit) before the network call,
    which runs outside any transaction. A failed check never clears an earlier verification.
    """
    async with db.begin():
        program = await _admin_program(db, user_id, program_id)
        await lock_key(db, f"origin-check:{program.id}")
        await enforce_rate_limit(
            db,
            _recent_checks(program.id),
            limit=CHECKS_PER_MINUTE,
            code="ORIGIN_VERIFY_RATE_LIMITED",
        )
        revision = await repo.get_revision(db, program)
        origin = origin_of(revision.base_url)
        row = await repo.ensure_verification(db, program.id, origin, _new_token())
        token, policy = row.token, _policy(revision)
        await record_audit(
            db,
            actor_id=user_id,
            org_id=program.owner_org_id,
            action=CHECK_ACTION,
            target=program.id,
            detail={"origin": origin},
        )

    reason = await _check_reason(policy, token)

    async with db.begin():
        row = await repo.ensure_verification(db, program.id, origin, token, for_update=True)
        now = datetime.now(UTC)
        row.last_checked_at = now
        if reason is None:
            row.verified_at = now
            await record_audit(
                db,
                actor_id=user_id,
                org_id=program.owner_org_id,
                action=VERIFIED_ACTION,
                target=program.id,
                detail={"origin": origin},
            )
        await db.flush()
    if reason is not None:
        raise _failed(reason)
    return row
