"""Factory that gives every service the same FastAPI setup."""

from fastapi import FastAPI

from farm_common.errors import register_error_handlers
from farm_common.log import CorrelationIdMiddleware, configure_logging
from farm_common.settings import ServiceSettings, get_settings


def create_app(
    service_name: str,
    version: str,
    settings: ServiceSettings | None = None,
    configure_logs: bool = True,
) -> FastAPI:
    """Create a FastAPI app with the behaviour shared by all services:

    - GET /health
    - X-Correlation-ID on every request and response, included in every log line
    - JSON logs on stdout
    - the standard error format for AppError, HTTP errors, validation errors and crashes
    """
    settings = settings or get_settings()
    if configure_logs:
        configure_logging(service_name, settings.log_level)

    app = FastAPI(
        title=f"{service_name} service",
        version=version,
        root_path=settings.root_path,
    )
    app.add_middleware(CorrelationIdMiddleware)
    register_error_handlers(app)

    @app.get("/health", tags=["ops"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": service_name, "version": version}

    return app
