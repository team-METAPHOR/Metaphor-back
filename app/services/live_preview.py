"""Live preview of a running job (`WS /validation-jobs/{id}/live`): access and relay state.

Readers are active members of the report's org (any role); everyone else gets NOT_FOUND, so a
job id from another org (or an engine id) reveals nothing. The worker writes the preview row
(`app/worker/live.py`); this side only reads it and decides what the viewer may be shown.
"""

import secrets
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.models import LiveTicket
from app.repositories import live_previews as repo
from app.services import auth
from app.services.limits import enforce_rate_limit, lock_key, since
from app.services.validation_jobs import load_visible_job

SCHEMA = "live-relay-1"
# Running job without a preview row after this long: the relay never came up.
STARTING_GRACE_SECONDS = 10
# The worker relay heartbeats every 5 s; three missed beats = relay down.
STALE_SECONDS = 15
MAX_CONNECTIONS_PER_USER = 4

# Direct `wss://` connections carry a one-time ticket instead of the dashboard's cookie:
# Sec-WebSocket-Protocol: metaphor-live.v1, ticket.<ticket>. Only PROTOCOL is ever echoed.
PROTOCOL = "metaphor-live.v1"
TICKET_PREFIX = "ticket."
TICKET_TTL = timedelta(seconds=30)
TICKETS_PER_MINUTE = 30  # per user; reconnects with backoff from a few tabs stay far below
TICKET_RETENTION = timedelta(minutes=10)
MAX_TICKET_LENGTH = 128

# Preview states (`relay_state.preview`).
QUEUED = "queued"  # job not started yet
STARTING = "starting"  # job claimed, relay not up yet
WAITING = "waiting"  # relay up, no engine stream yet (context lock, condition selection)
LIVE = "live"
DISABLED = "disabled"  # worker runs without QA_LIVE_ENABLED
RELAY_UNAVAILABLE = "relay_unavailable"
WORKER_LOST = "worker_lost"  # lease expired: the worker process is gone
CANCELLING = "cancelling"
ENDED = "ended"  # job is terminal; use the job/result APIs
STREAMING = (WAITING, LIVE)


@dataclass(frozen=True)
class LiveAccess:
    user_id: str
    job_id: str
    report_id: str
    # Kept for the periodic re-check, whichever way (cookie or ticket) the viewer connected.
    session_id: str


@dataclass(frozen=True)
class TicketGrant:
    ticket: str
    expires_at: datetime


@dataclass(frozen=True)
class TicketBinding:
    session_id: str
    user_id: str
    org_id: str


@dataclass(frozen=True)
class LiveView:
    job_status: str
    preview: str
    stream: int
    condition_id: str | None
    version: int | None
    updated_at: datetime | None
    # Only when the row changed since the caller's version, and only while streaming.
    messages: list[dict] | None
    frame: bytes | None
    frame_sequence: int | None


async def authorize(
    db: AsyncSession, session_id: str | None, job_id: str, binding: TicketBinding | None = None
) -> LiveAccess:
    """401 UNAUTHENTICATED (no/expired session) or 404 NOT_FOUND (no access), as for REST.

    `session_id` is the stored session id (sha256 of the cookie token). With a ticket
    `binding`, the job must also still belong to the org and user the ticket was issued for."""
    user = await auth.user_for_session(db, session_id) if session_id else None
    if user is None or (binding is not None and binding.user_id != user.id):
        raise ApiError(401, "UNAUTHENTICATED", "Login required")
    async with db.begin():
        job, report = await load_visible_job(db, user.id, job_id)
        if binding is not None and report.org_id != binding.org_id:
            raise ApiError(404, "NOT_FOUND", "Not found")
        return LiveAccess(
            user_id=user.id, job_id=job.id, report_id=report.id, session_id=session_id
        )


async def issue_ticket(db: AsyncSession, user_id: str, session_id: str, job_id: str) -> TicketGrant:
    """A 30 s one-time ticket for `job_id`, bound to this user, session, org and job.

    404 like every job read for non-members; 429 LIVE_TICKET_THROTTLED past
    TICKETS_PER_MINUTE per user. The raw ticket is returned once and only its hash is stored."""
    ticket = secrets.token_urlsafe(32)
    async with db.begin():
        job, report = await load_visible_job(db, user_id, job_id)
        await lock_key(db, f"live_ticket:{user_id}")  # count-then-insert is atomic per user
        await enforce_rate_limit(
            db,
            repo.count_user_tickets(user_id, since(minutes=1)),
            limit=TICKETS_PER_MINUTE,
            code="LIVE_TICKET_THROTTLED",
            message="실시간 화면 연결 요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.",
        )
        await repo.prune_user_tickets(db, user_id, datetime.now(UTC) - TICKET_RETENTION)
        expires_at = datetime.now(UTC) + TICKET_TTL
        await repo.add_ticket(
            db,
            LiveTicket(
                token_hash=auth.token_hash(ticket),
                session_id=session_id,
                user_id=user_id,
                org_id=report.org_id,
                job_id=job.id,
                expires_at=expires_at,
            ),
        )
    return TicketGrant(ticket=ticket, expires_at=expires_at)


async def consume_ticket(db: AsyncSession, ticket: str, job_id: str) -> TicketBinding:
    """Burn the ticket (exactly once, even under concurrent handshakes) and return its binding.

    401 TICKET_INVALID for an unknown, expired, already used or revoked-session ticket (one
    reason, so nothing is learned by probing). A ticket for another job is burned too, then
    404 NOT_FOUND like any job the caller cannot see."""
    if not ticket or len(ticket) > MAX_TICKET_LENGTH:
        raise ApiError(401, "TICKET_INVALID", "Connection ticket is invalid or expired")
    async with db.begin():
        row = await repo.consume_ticket(db, auth.token_hash(ticket))
    if row is None:
        raise ApiError(401, "TICKET_INVALID", "Connection ticket is invalid or expired")
    if row.job_id != job_id:
        raise ApiError(404, "NOT_FOUND", "Not found")
    return TicketBinding(session_id=row.session_id, user_id=row.user_id, org_id=row.org_id)


def preview_state(row) -> str:
    if row.status == "queued":
        return QUEUED
    if row.status == "cancelling":
        return CANCELLING
    if row.status != "running":
        return ENDED
    if row.lease_expired:
        return WORKER_LOST
    if row.version is None:
        started = row.started_at or row.now
        fresh = (row.now - started).total_seconds() < STARTING_GRACE_SECONDS
        return STARTING if fresh else RELAY_UNAVAILABLE
    if not row.enabled:
        return DISABLED
    if (row.now - row.updated_at).total_seconds() > STALE_SECONDS:
        return RELAY_UNAVAILABLE
    return LIVE if row.stream else WAITING


async def read(db: AsyncSession, job_id: str, known_version: int | None) -> LiveView:
    async with db.begin():
        row = await repo.snapshot(db, job_id, known_version)
    if row is None:  # job deleted (report purge): same as gone
        return LiveView("failed", ENDED, 0, None, None, None, None, None, None)
    preview = preview_state(row)
    streaming = preview in STREAMING
    return LiveView(
        job_status=row.status,
        preview=preview,
        stream=row.stream or 0,
        condition_id=row.condition_id,
        version=row.version,
        updated_at=row.updated_at,
        # A stale or orphaned row (worker lost, cancelling) is never forwarded as live.
        messages=row.messages if streaming else None,
        frame=row.frame if streaming else None,
        frame_sequence=row.frame_sequence if streaming else None,
    )


class ConnectionLimit:
    """Open live connections per user, in this API process.

    ponytail: per-process counter; a shared limit would need a table or the session row."""

    def __init__(self, limit: int = MAX_CONNECTIONS_PER_USER):
        self.limit = limit
        self._open: Counter[str] = Counter()

    def acquire(self, user_id: str) -> bool:
        if self._open[user_id] >= self.limit:
            return False
        self._open[user_id] += 1
        return True

    def release(self, user_id: str) -> None:
        self._open[user_id] -= 1
        if self._open[user_id] <= 0:
            del self._open[user_id]


connections = ConnectionLimit()
