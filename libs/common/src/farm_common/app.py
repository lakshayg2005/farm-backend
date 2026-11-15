"""Factory that gives every service the same FastAPI setup."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from fastapi import FastAPI

from farm_common.errors import register_error_handlers
from farm_common.log import CorrelationIdMiddleware, configure_logging
from farm_common.settings import ServiceSettings, get_settings

Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


def create_app(
    service_name: str,
    version: str,
    settings: ServiceSettings | None = None,
    configure_logs: bool = True,
    lifespan: Lifespan | None = None,
) -> FastAPI:
    """Create a FastAPI app with the behaviour shared by all services:

    - GET /health
    - X-Correlation-ID on every request and response, included in every log line
    - JSON logs on stdout
    - the standard error format for AppError, HTTP errors, validation errors and crashes

    `lifespan` runs on startup/shutdown, e.g. to connect the event bus and start the outbox relay.
    """
    settings = settings or get_settings()
    if configure_logs:
        configure_logging(service_name, settings.log_level)

    app = FastAPI(
        title=f"{service_name} service",
        version=version,
        root_path=settings.root_path,
        lifespan=lifespan,
    )
    app.add_middleware(CorrelationIdMiddleware)
    register_error_handlers(app)

    @app.get("/health", tags=["ops"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": service_name, "version": version}

    return app
