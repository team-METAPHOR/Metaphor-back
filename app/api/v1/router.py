from fastapi import APIRouter, Depends

from app.api.dependencies import require_csrf
from app.api.v1.endpoints import (
    auth,
    evaluation,
    evidence,
    health,
    live_preview,
    orgs,
    programs,
    reports,
    test_accounts,
    test_conditions,
    validation_jobs,
    widget,
    widget_settings,
)

api_router = APIRouter()
api_router.include_router(health.router)
# Public Web SDK API: no cookie auth, no CSRF guard (per-program Origin checks instead).
api_router.include_router(widget.router)
# WebSocket: checks Origin and the session cookie or one-time ticket itself (require_csrf is
# HTTP-only; the POST live-ticket route declares it on the route).
api_router.include_router(live_preview.router)

# Cookie-auth routes. /widget/* mounts on api_router directly: no cookies, no CSRF guard.
platform_router = APIRouter(dependencies=[Depends(require_csrf)])
platform_router.include_router(auth.router)
platform_router.include_router(orgs.router)
platform_router.include_router(programs.router)
platform_router.include_router(test_conditions.router)
platform_router.include_router(widget_settings.router)
platform_router.include_router(test_accounts.router)
platform_router.include_router(reports.router)
platform_router.include_router(validation_jobs.router)
platform_router.include_router(evidence.router)
platform_router.include_router(evaluation.router)
api_router.include_router(platform_router)
