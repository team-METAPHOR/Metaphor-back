from datetime import datetime

from pydantic import BaseModel


class LiveTicketOut(BaseModel):
    """One-time live connection ticket. Never log or put `ticket` in a URL."""

    # wss:// URL of the live endpoint on PUBLIC_API_BASE; null when it is unset (then connect
    # to the same-origin path /api/v1/validation-jobs/{id}/live, still offering the ticket).
    websocket_url: str | None
    ticket: str
    expires_at: datetime
    # Offer [subprotocol, "ticket." + ticket]; the server selects only `subprotocol`.
    subprotocol: str
