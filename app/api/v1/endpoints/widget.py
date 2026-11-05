"""Public Web SDK routes. Mounted on api_router directly: no cookies, no CSRF guard."""

from typing import Annotated

from fastapi import APIRouter, File, Form, Header, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse

from app.api.dependencies import DbSession
from app.core.client_ip import client_ip
from app.core.errors import ApiError
from app.core.widget_cors import PREFLIGHT_HEADERS
from app.schemas.widget import WidgetConfig, WidgetReceipt
from app.services import widget as service
from app.services.intake import read_upload_capped

router = APIRouter(prefix="/widget", tags=["widget"])
ProjectKey = Annotated[str, Query(min_length=1, max_length=64)]


async def _allow_origin(request: Request, db, project_key: str) -> str:
    origin = request.headers.get("origin")
    await service.check_origin(db, project_key, origin)
    request.state.widget_origin = origin
    return origin


async def _preflight(request: Request, db, project_key: str) -> Response:
    try:
        await _allow_origin(request, db, project_key)
    except ApiError:
        # Unknown key and foreign origin look the same to a preflight.
        raise ApiError(403, "ORIGIN_NOT_ALLOWED", "허용되지 않은 사이트입니다.") from None
    return Response(status_code=204, headers=PREFLIGHT_HEADERS)


@router.options("/config", include_in_schema=False)
async def config_preflight(request: Request, db: DbSession, project_key: ProjectKey):
    return await _preflight(request, db, project_key)


@router.options("/reports", include_in_schema=False)
async def reports_preflight(request: Request, db: DbSession, project_key: ProjectKey):
    return await _preflight(request, db, project_key)


@router.get("/config", response_model=WidgetConfig)
async def widget_config(
    request: Request, response: Response, db: DbSession, project_key: ProjectKey
):
    origin = await _allow_origin(request, db, project_key)
    response.headers["Cache-Control"] = "public, max-age=60"
    return await service.get_config(db, project_key, origin)


@router.post("/reports", status_code=201, response_model=WidgetReceipt)
async def widget_report(
    request: Request,
    db: DbSession,
    project_key: ProjectKey,
    payload: Annotated[str, Form(max_length=service.MAX_PAYLOAD_BYTES)],
    screenshot: Annotated[UploadFile | None, File()] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    origin = await _allow_origin(request, db, project_key)
    image = await read_upload_capped(screenshot) if screenshot is not None else None
    status, body = await service.submit_report(
        db,
        project_key=project_key,
        origin=origin,
        client_ip=client_ip(request),
        raw_payload=payload,
        image=image,
        idempotency_key=idempotency_key,
    )
    return JSONResponse(body, status_code=status)
