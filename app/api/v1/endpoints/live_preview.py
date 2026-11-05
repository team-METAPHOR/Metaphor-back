"""`WS /api/v1/validation-jobs/{job_id}/live`: masked live preview of a running job.

Observation only: client messages are ignored. Protocol: docs/api/live-preview.md.
`POST .../live-ticket` issues the one-time ticket a direct `wss://` connection (no cookie on
the API host) offers in Sec-WebSocket-Protocol. Never log tickets or put them in URLs.
"""

import asyncio
import json
import logging
import time
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request, Response, WebSocket, WebSocketDisconnect

from app.api.dependencies import SESSION_COOKIE, CurrentUser, DbSession, require_csrf
from app.core.config import get_settings
from app.core.errors import ApiError
from app.integrations import live_notify
from app.schemas.live_preview import LiveTicketOut
from app.services import live_preview as service
from app.services.auth import token_hash

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/validation-jobs", tags=["validation-jobs"])

POLL_SECONDS = 1.0
HEARTBEAT_SECONDS = 15.0
RECHECK_SECONDS = 10.0
SEND_TIMEOUT_SECONDS = 5.0

# Application close codes (4000-4999). 1000 = job ended normally.
CLOSE_CODES = {401: 4401, 404: 4404}  # 401 reason: UNAUTHENTICATED or TICKET_INVALID
CLOSE_TOO_MANY = 4429
CLOSE_SLOW = 4408


def _iso(moment: datetime | None) -> str | None:
    return moment.astimezone(UTC).isoformat() if moment else None


def _control(kind: str, **fields) -> str:
    return json.dumps({"schema_version": service.SCHEMA, "kind": kind, **fields})


class _Viewer:
    def __init__(self, websocket: WebSocket, access: service.LiveAccess):
        self.websocket, self.access = websocket, access
        self.version: int | None = None
        self.state: tuple | None = None
        self.stream = 0
        self.cursor = 0

    async def send(self, payload: str | bytes) -> None:
        # A viewer that stops reading only loses its own connection.
        async with asyncio.timeout(SEND_TIMEOUT_SECONDS):
            if isinstance(payload, bytes):
                await self.websocket.send_bytes(payload)
            else:
                await self.websocket.send_text(payload)

    async def push(self, view: service.LiveView) -> None:
        state = (view.preview, view.job_status, view.stream, view.condition_id)
        if view.stream != self.stream:
            self.stream, self.cursor = view.stream, 0  # engine sequences restart per stream
        if state != self.state:
            self.state = state
            await self.send(
                _control(
                    "relay_state",
                    job_id=self.access.job_id,
                    report_id=self.access.report_id,
                    job_status=view.job_status,
                    preview=view.preview,
                    stream=view.stream,
                    condition_id=view.condition_id,
                    updated_at=_iso(view.updated_at),
                )
            )
        if view.messages is None and view.frame is None:
            return
        self.version = view.version
        items: list[tuple[int, str | bytes]] = [
            (m["sequence"], json.dumps(m, separators=(",", ":")))
            for m in view.messages or ()
            if m.get("sequence", 0) > self.cursor
        ]
        if view.frame is not None and (view.frame_sequence or 0) > self.cursor:
            items.append((view.frame_sequence, bytes(view.frame)))
        for sequence, payload in sorted(items, key=lambda item: item[0]):
            await self.send(payload)
            self.cursor = sequence


def _websocket_url(job_id: str) -> str | None:
    base = get_settings().public_api_base
    if not base:
        return None
    scheme, sep, rest = base.partition("://")
    ws_scheme = {"https": "wss", "http": "ws"}.get(scheme.lower(), scheme)
    return f"{ws_scheme}{sep}{rest}/api/v1/validation-jobs/{job_id}/live"


@router.post(
    "/{job_id}/live-ticket",
    response_model=LiveTicketOut,
    dependencies=[Depends(require_csrf)],
)
async def issue_live_ticket(
    job_id: str, request: Request, response: Response, user: CurrentUser, db: DbSession
):
    """One-time (30 s) ticket for `WS .../live`, for any active member of the job's org."""
    session_id = token_hash(request.cookies[SESSION_COOKIE])  # CurrentUser checked it
    grant = await service.issue_ticket(db, user.id, session_id, job_id)
    response.headers["Cache-Control"] = "no-store"
    return LiveTicketOut(
        websocket_url=_websocket_url(job_id),
        ticket=grant.ticket,
        expires_at=grant.expires_at,
        subprotocol=service.PROTOCOL,
    )


def _offered_ticket(websocket: WebSocket) -> tuple[bool, str | None]:
    """(well_formed, ticket) from Sec-WebSocket-Protocol. A ticket needs PROTOCOL beside it."""
    offered = websocket.scope.get("subprotocols") or []
    prefix = service.TICKET_PREFIX
    tickets = [p.removeprefix(prefix) for p in offered if p.startswith(prefix)]
    if not tickets:
        return True, None
    return len(tickets) == 1 and service.PROTOCOL in offered, tickets[0]


@router.websocket("/{job_id}/live")
async def live_preview(websocket: WebSocket, job_id: str, db: DbSession):
    # Cross-site WebSocket hijacking guard: browsers always send Origin on WebSockets.
    well_formed, ticket = _offered_ticket(websocket)
    origin_ok = websocket.headers.get("origin") in get_settings().allowed_origins
    if not origin_ok or not well_formed:
        await websocket.close(code=1008)  # before accept → handshake rejected (HTTP 403)
        return
    offered = websocket.scope.get("subprotocols") or []
    # Select only the fixed protocol (if offered): the ticket is never echoed back.
    await websocket.accept(subprotocol=service.PROTOCOL if service.PROTOCOL in offered else None)
    try:
        if ticket is not None:
            binding = await service.consume_ticket(db, ticket, job_id)
            access = await service.authorize(db, binding.session_id, job_id, binding)
        else:
            token = websocket.cookies.get(SESSION_COOKIE)
            access = await service.authorize(db, token_hash(token) if token else None, job_id)
    except ApiError as error:
        await websocket.close(code=CLOSE_CODES.get(error.status, 4404), reason=error.code)
        return
    if not service.connections.acquire(access.user_id):
        await websocket.close(code=CLOSE_TOO_MANY, reason="TOO_MANY_CONNECTIONS")
        return
    wake = asyncio.Event()
    notifier = live_notify.notifier()
    notifier.subscribe(access.job_id, wake)
    receiver = asyncio.create_task(_drain_client(websocket))
    try:
        await _serve(websocket, db, _Viewer(websocket, access), wake, receiver)
    except (WebSocketDisconnect, RuntimeError, TimeoutError):
        pass  # client went away or stopped reading mid-send
    finally:
        notifier.unsubscribe(access.job_id, wake)
        service.connections.release(access.user_id)
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)


async def _drain_client(websocket: WebSocket) -> None:
    """Read (and ignore) client frames so a disconnect is noticed."""
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return


async def _serve(websocket, db, viewer: _Viewer, wake, receiver) -> None:
    access = viewer.access
    checked = beat = time.monotonic()
    while True:
        wake.clear()
        view = await service.read(db, access.job_id, viewer.version)
        try:
            await viewer.push(view)
        except TimeoutError:
            await websocket.close(code=CLOSE_SLOW, reason="SLOW_CONSUMER")
            return
        if view.preview == service.ENDED:
            await websocket.close(code=1000, reason="JOB_ENDED")
            return
        now = time.monotonic()
        if now - checked >= RECHECK_SECONDS:
            checked = now
            try:  # session revoked, membership removed, org deactivated → close at once
                await service.authorize(db, access.session_id, access.job_id)
            except ApiError as error:
                await websocket.close(code=CLOSE_CODES.get(error.status, 4404), reason=error.code)
                return
        if now - beat >= HEARTBEAT_SECONDS:
            beat = now
            await viewer.send(_control("heartbeat", at=datetime.now(UTC).isoformat()))
        waiter = asyncio.create_task(wake.wait())
        done, _ = await asyncio.wait(
            {waiter, receiver}, timeout=POLL_SECONDS, return_when=asyncio.FIRST_COMPLETED
        )
        waiter.cancel()
        if receiver in done:
            return
