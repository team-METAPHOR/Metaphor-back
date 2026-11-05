"""Public Web SDK bundle: GET/HEAD /sdk/v1/metaphor.js and /sdk/v1/metaphor-<version>.js.

No auth, no CSRF, no cookies. Any origin may load it (`<script integrity crossorigin>`).
"""

from fastapi import APIRouter, Request, Response

from app.core.errors import ApiError
from app.core.sdk_bundle import SdkBundle, get_bundle

router = APIRouter(prefix="/sdk/v1", tags=["sdk"])

LATEST_CACHE = "public, max-age=300"
PINNED_CACHE = "public, max-age=31536000, immutable"


def _require_bundle() -> SdkBundle:
    bundle = get_bundle()
    if bundle is None:
        raise ApiError(503, "SDK_UNAVAILABLE", "SDK 번들을 사용할 수 없습니다.")
    return bundle


def _serve(request: Request, bundle: SdkBundle, cache_control: str) -> Response:
    headers = {
        "Cache-Control": cache_control,
        "ETag": bundle.etag,
        "Access-Control-Allow-Origin": "*",
        "Cross-Origin-Resource-Policy": "cross-origin",
        "X-Content-Type-Options": "nosniff",
    }
    if request.headers.get("if-none-match") == bundle.etag:
        return Response(status_code=304, headers=headers)
    return Response(bundle.body, media_type="text/javascript; charset=utf-8", headers=headers)


@router.api_route("/metaphor.js", methods=["GET", "HEAD"], include_in_schema=False)
async def latest_sdk(request: Request) -> Response:
    return _serve(request, _require_bundle(), LATEST_CACHE)


@router.api_route("/metaphor-{version}.js", methods=["GET", "HEAD"], include_in_schema=False)
async def pinned_sdk(version: str, request: Request) -> Response:
    bundle = _require_bundle()
    if version != bundle.version:
        raise ApiError(404, "NOT_FOUND", "요청한 SDK 버전이 없습니다.")
    return _serve(request, bundle, PINNED_CACHE)
