from fastapi import Request, Response

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


def apply_security_headers(path: str, response: Response) -> None:
    response.headers.update(SECURITY_HEADERS)
    if path.startswith("/api/") and "cache-control" not in response.headers:
        response.headers["Cache-Control"] = "no-store"


async def security_headers_middleware(request: Request, call_next) -> Response:
    response = await call_next(request)
    apply_security_headers(request.url.path, response)
    return response
