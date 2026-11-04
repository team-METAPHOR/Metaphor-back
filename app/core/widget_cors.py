"""CORS and size guard for /api/v1/widget/* only.

Endpoints validate the Origin against the program's allowed_origins and record it on
`request.state.widget_origin`; only then is it echoed. Allow-Credentials is never sent.
"""

from fastapi import Request, Response

from app.core.errors import error_response

WIDGET_PREFIX = "/api/v1/widget/"
MAX_REQUEST_BYTES = 5 * 1024 * 1024
PREFLIGHT_HEADERS = {
    "Access-Control-Allow-Methods": "GET, POST",
    "Access-Control-Allow-Headers": "Content-Type, Idempotency-Key",
    "Access-Control-Max-Age": "600",
}


def _too_large(request: Request) -> Response | None:
    if request.method != "POST":
        return None
    length = request.headers.get("content-length")
    if length is None:
        return error_response(request, 411, "LENGTH_REQUIRED", "Content-Length가 필요합니다.")
    if not length.isdigit() or int(length) > MAX_REQUEST_BYTES:
        return error_response(request, 413, "REQUEST_TOO_LARGE", "요청이 너무 큽니다.")
    return None


async def widget_cors_middleware(request: Request, call_next) -> Response:
    if not request.url.path.startswith(WIDGET_PREFIX):
        return await call_next(request)
    # Checked before the body is read (FastAPI parses forms before the endpoint runs).
    response = _too_large(request) or await call_next(request)
    origin = getattr(request.state, "widget_origin", None)
    if origin:
        response.headers["Access-Control-Allow-Origin"] = origin
    vary = response.headers.get("Vary")
    if not vary or "origin" not in vary.lower():
        response.headers["Vary"] = f"{vary}, Origin" if vary else "Origin"
    if "access-control-allow-credentials" in response.headers:
        del response.headers["access-control-allow-credentials"]
    return response
