from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.sdk import router as sdk_router
from app.api.v1.router import api_router
from app.core.body_limit import BodyLimitMiddleware
from app.core.config import get_settings
from app.core.errors import register_exception_handlers, request_id_middleware
from app.core.hashing import check_hash_secret
from app.core.logging import configure_logging
from app.core.security_headers import security_headers_middleware
from app.core.widget_cors import widget_cors_middleware
from app.db.session import engine


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    check_hash_secret(get_settings())
    try:
        yield
    finally:
        await engine.dispose()


settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    debug=settings.debug,
    lifespan=lifespan,
)
# Added last = outermost: request id wraps everything, including security headers.
app.add_middleware(BodyLimitMiddleware)
app.middleware("http")(widget_cors_middleware)
app.middleware("http")(security_headers_middleware)
app.middleware("http")(request_id_middleware)
register_exception_handlers(app)
app.include_router(api_router, prefix="/api/v1")
# Public Web SDK bundle, outside /api: no auth, no CSRF, cacheable.
app.include_router(sdk_router)
