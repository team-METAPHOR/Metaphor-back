"""Reject oversized requests by Content-Length before any body parsing."""

from fastapi import Request, Response

from app.core.errors import error_response

# Screenshots are capped at 4 MiB; the rest is multipart overhead and form fields.
MAX_REQUEST_BYTES = 6 * 1024 * 1024


async def body_limit_middleware(request: Request, call_next) -> Response:
    """413 for a declared Content-Length over MAX_REQUEST_BYTES, before the body is read.

    ponytail: chunked bodies without Content-Length are left to the reverse proxy's body limit
    (e.g. nginx client_max_body_size); uploads are also capped again while reading.
    """
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_REQUEST_BYTES:
        return error_response(request, 413, "PAYLOAD_TOO_LARGE", "요청 본문이 너무 큽니다.")
    return await call_next(request)
