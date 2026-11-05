"""Cap request bodies by the bytes actually received, not only by Content-Length.

Pure ASGI middleware: wraps `receive` and counts every `http.request` chunk, so chunked or
length-less bodies cannot slip past. A reverse-proxy limit stays as defense in depth only.
Per-route upload caps (`read_upload_capped`) still apply below this global limit.
"""

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import error_response

# Screenshots are capped at 4 MiB; the rest is multipart overhead and form fields.
MAX_REQUEST_BYTES = 6 * 1024 * 1024


class _BodyTooLarge(Exception):
    pass


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int = MAX_REQUEST_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        length = Request(scope).headers.get("content-length")
        if length and length.isdigit() and int(length) > self.max_bytes:
            await self._reject(scope, receive, send)
            return

        received = 0
        response_started = False
        rejected = False

        async def limited_receive() -> Message:
            nonlocal received, rejected
            if rejected:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    rejected = True
                    if not response_started:
                        await self._reject(scope, receive, send)
                    raise _BodyTooLarge
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            if rejected:
                return  # our 413 already went out; drop whatever downstream sends
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _BodyTooLarge:
            if response_started:
                raise
        except Exception:
            # Frameworks may wrap our signal (e.g. FastAPI turns body errors into 400).
            if not rejected:
                raise

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        response = error_response(
            Request(scope), 413, "PAYLOAD_TOO_LARGE", "요청 본문이 너무 큽니다."
        )
        response.headers["Connection"] = "close"
        await response(scope, receive, send)
