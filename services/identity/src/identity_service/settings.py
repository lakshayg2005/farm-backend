from functools import lru_cache
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from pydantic import model_validator

from farm_common.settings import ServiceSettings


class IdentitySettings(ServiceSettings):
    # RS256 private key used to SIGN tokens (only identity has it). Generate a pair with
    # `uv run python scripts/gen_jwt_keys.py`. The public key is derived from it if not given.
    jwt_private_key: str | None = None
    jwt_private_key_path: str | None = None

    access_token_minutes: int = 15
    refresh_token_days: int = 7

    otp_ttl_seconds: int = 300
    otp_resend_seconds: int = 30
    otp_max_attempts: int = 5
    # Mock SMS: return the OTP in the API response (and log it) instead of sending a text.
    otp_dev_mode: bool = True

    # Publish events to RabbitMQ through the outbox relay.
    events_enabled: bool = True

    @model_validator(mode="after")
    def _load_keys(self) -> "IdentitySettings":
        if self.environment == "production" and self.otp_dev_mode:
            raise ValueError("OTP_DEV_MODE must be false in production")
        if not self.jwt_private_key and self.jwt_private_key_path:
            self.jwt_private_key = Path(self.jwt_private_key_path).read_text(encoding="utf-8")
        if self.jwt_private_key and not (self.jwt_public_key or self.jwt_public_key_path):
            key = serialization.load_pem_private_key(self.jwt_private_key.encode(), password=None)
            self.jwt_public_key = (
                key.public_key()
                .public_bytes(
                    serialization.Encoding.PEM,
                    serialization.PublicFormat.SubjectPublicKeyInfo,
                )
                .decode()
            )
        return self

    def private_key(self) -> str:
        if not self.jwt_private_key:
            raise RuntimeError(
                "No JWT signing key: set JWT_PRIVATE_KEY_PATH "
                "(generate one with `uv run python scripts/gen_jwt_keys.py`)"
            )
        return self.jwt_private_key


@lru_cache
def get_identity_settings() -> IdentitySettings:
    return IdentitySettings()
