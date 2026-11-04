from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.errors import register_exception_handlers, request_id_middleware
from app.core.logging import configure_logging
from app.core.security_headers import security_headers_middleware
from app.db.session import engine


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
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
app.middleware("http")(security_headers_middleware)
app.middleware("http")(request_id_middleware)
register_exception_handlers(app)
app.include_router(api_router, prefix="/api/v1")
