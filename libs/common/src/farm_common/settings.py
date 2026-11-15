"""Settings every service reads from environment variables (or a local .env file)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class ServiceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "local"
    log_level: str = "INFO"
    # Each service connects with its own database user, limited to its own schema.
    database_url: str = "postgresql+asyncpg://farm:change-me@localhost:5432/farm"
    rabbitmq_url: str = "amqp://farm:change-me@localhost:5672/"
    redis_url: str = "redis://localhost:6379/0"
    # Set to /api/v1/<service> behind the gateway so Swagger UI links resolve.
    root_path: str = ""

    # JWT verification (tokens are signed by the identity service with RS256).
    # Give the PEM text directly, or a path to the PEM file.
    jwt_public_key: str | None = None
    jwt_public_key_path: str | None = None
    jwt_issuer: str = "farm-identity"
    jwt_audience: str = "farm-platform"


@lru_cache
def get_settings() -> ServiceSettings:
    """Cached settings; override in tests with app.dependency_overrides[get_settings]."""
    return ServiceSettings()
