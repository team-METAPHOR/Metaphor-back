"""Live preview of a running job (`WS /validation-jobs/{id}/live`): access and relay state.

Readers are active members of the report's org (any role); everyone else gets NOT_FOUND, so a
job id from another org (or an engine id) reveals nothing. The worker writes the preview row
(`app/worker/live.py`); this side only reads it and decides what the viewer may be shown.
"""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.repositories import live_previews as repo
from app.services import auth
from app.services.validation_jobs import load_visible_job

SCHEMA = "live-relay-1"
# Running job without a preview row after this long: the relay never came up.
STARTING_GRACE_SECONDS = 10
# The worker relay heartbeats every 5 s; three missed beats = relay down.
STALE_SECONDS = 15
MAX_CONNECTIONS_PER_USER = 4

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


async def authorize(db: AsyncSession, token: str | None, job_id: str) -> LiveAccess:
    """401 UNAUTHENTICATED (no/expired session) or 404 NOT_FOUND (no access), as for REST."""
    user = await auth.user_for_token(db, token) if token else None
    if user is None:
        raise ApiError(401, "UNAUTHENTICATED", "Login required")
    async with db.begin():
        job, report = await load_visible_job(db, user.id, job_id)
        return LiveAccess(user_id=user.id, job_id=job.id, report_id=report.id)


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
