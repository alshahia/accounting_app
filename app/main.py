"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1 import api_router
from app.config import Settings, get_settings
from app.shared.db import Database
from app.shared.errors import install_error_handlers
from app.shared.logging import configure_logging
from app.shared.middleware import RequestContextMiddleware
from app.shared.ratelimit import FixedWindowLimiter
from app.shared.security_headers import SecurityHeadersMiddleware
from app.shared.static_web import mount_web


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application; tests pass explicit settings, production uses env."""
    resolved = settings if settings is not None else get_settings()
    configure_logging(resolved.log_level)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncGenerator[None]:
        database = Database(
            resolved.database_url.get_secret_value(),
            set_role=resolved.database_set_role or None,
        )
        application.state.database = database
        try:
            yield
        finally:
            await database.dispose()

    docs_enabled = resolved.docs_are_enabled
    application = FastAPI(
        title=resolved.app_name,
        version=resolved.version,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url=f"{resolved.api_prefix}/openapi.json" if docs_enabled else None,
        lifespan=lifespan,
    )
    application.state.settings = resolved
    application.state.login_limiter = FixedWindowLimiter(resolved.auth_rate_limit_per_minute)
    application.add_middleware(
        SecurityHeadersMiddleware,
        hsts=resolved.environment == "production",
    )
    application.add_middleware(RequestContextMiddleware)
    application.include_router(api_router, prefix=resolved.api_prefix)
    install_error_handlers(application)
    # In the shipped image the same process serves the compiled frontend. Tests
    # and the Vite development server do not rely on this path, and an API-only
    # deployment simply has no bundle to mount.
    if resolved.environment != "test":
        mount_web(application)
    return application


app = create_app()
