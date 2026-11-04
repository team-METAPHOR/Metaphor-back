from fastapi import APIRouter, Depends

from app.api.dependencies import require_csrf
from app.api.v1.endpoints import (
    auth,
    health,
    programs,
    reports,
    validation_jobs,
    widget_settings,
)

api_router = APIRouter()
api_router.include_router(health.router)

# Cookie-auth routes. /widget/* mounts on api_router directly: no cookies, no CSRF guard.
platform_router = APIRouter(dependencies=[Depends(require_csrf)])
platform_router.include_router(auth.router)
platform_router.include_router(programs.router)
platform_router.include_router(widget_settings.router)
platform_router.include_router(reports.router)
platform_router.include_router(validation_jobs.router)
api_router.include_router(platform_router)
