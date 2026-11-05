from fastapi import APIRouter

from app.api.dependencies import CurrentUser, DbSession
from app.core.config import get_settings
from app.core.sdk_bundle import get_bundle
from app.models import ProgramWidgetSettings
from app.schemas.widget import (
    AllowedOrigin,
    ProjectKeyResponse,
    WidgetSettingsPatch,
    WidgetSettingsResponse,
)
from app.services import widget_settings as service

router = APIRouter(prefix="/programs/{program_id}/widget-settings", tags=["widget-settings"])


def _install_info() -> dict[str, str | None]:
    api_base = get_settings().public_api_base or None
    bundle = get_bundle()
    if api_base is None or bundle is None:
        return {"api_base": api_base, "sdk_url": None, "sdk_integrity": None}
    return {
        "api_base": api_base,
        "sdk_url": f"{api_base}/sdk/v1/metaphor-{bundle.version}.js",
        "sdk_integrity": bundle.integrity,
    }


def _response(
    program_id: str, row: ProgramWidgetSettings | None, base_url: str
) -> WidgetSettingsResponse:
    if row is None:
        return WidgetSettingsResponse(
            program_id=program_id,
            public_project_key=None,
            allowed_origins=[],
            widget_enabled=False,
            public_reporting_enabled=False,
            auto_detect_enabled=False,
            live_screenshot_enabled=False,
            updated_at=None,
            **_install_info(),
        )
    return WidgetSettingsResponse(
        program_id=program_id,
        public_project_key=row.public_project_key,
        allowed_origins=[
            AllowedOrigin(origin=o, kind=service.origin_kind(o, base_url))
            for o in row.allowed_origins
        ],
        widget_enabled=row.widget_enabled,
        public_reporting_enabled=row.public_reporting_enabled,
        auto_detect_enabled=row.auto_detect_enabled,
        live_screenshot_enabled=row.live_screenshot_enabled,
        updated_at=row.updated_at,
        **_install_info(),
    )


@router.get("", response_model=WidgetSettingsResponse)
async def get_widget_settings(program_id: str, user: CurrentUser, db: DbSession):
    return _response(program_id, *await service.get_settings(db, user.id, program_id))


@router.patch("", response_model=WidgetSettingsResponse)
async def update_widget_settings(
    program_id: str, body: WidgetSettingsPatch, user: CurrentUser, db: DbSession
):
    return _response(program_id, *await service.update_settings(db, user.id, program_id, body))


@router.post("/rotate-key", response_model=ProjectKeyResponse)
async def rotate_project_key(program_id: str, user: CurrentUser, db: DbSession):
    return ProjectKeyResponse(public_project_key=await service.rotate_key(db, user.id, program_id))
