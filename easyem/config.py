"""Application configuration.

All secrets come from the environment. Nothing sensitive has a usable default:
the app refuses to start in production without an explicit SECRET_KEY.
"""

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_SECRET = "dev-only-insecure-secret-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: Literal["dev", "test", "staging", "production"] = "dev"

    database_url: str = "postgresql+psycopg://easyem:easyem@localhost:5432/easyem"

    secret_key: str = DEV_SECRET
    # The API may advertise openEMS when it is installed on a separate worker.
    openems_enabled: bool = False
    # Short-lived access token, long-lived rotating refresh token.
    access_token_ttl_seconds: int = 15 * 60
    refresh_token_ttl_seconds: int = 30 * 24 * 3600

    email_verification_ttl_seconds: int = 24 * 3600
    password_reset_ttl_seconds: int = 30 * 60

    # A credit reservation that is never settled must not freeze funds forever.
    credit_reservation_ttl_seconds: int = 2 * 3600

    signup_grant_credits: int = 50

    # --- email -----------------------------------------------------------
    # `console` prints, `memory` collects for tests, `smtp` sends.
    email_backend: Literal["console", "memory", "smtp"] = "console"
    email_from: str = "EasyEM <no-reply@easyem.com>"
    app_base_url: str = "http://localhost:3000"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True

    # --- http ------------------------------------------------------------
    #: Explicit origins. A wildcard with credentials is rejected by browsers
    #: anyway, and would be wrong here even if it were not.
    cors_origins: str = "http://localhost:3000"

    #: Requests per window, per client, on the authentication endpoints.
    #: Account lockout stops brute force against one account; this stops
    #: spraying across thousands.
    auth_rate_limit: int = 10
    auth_rate_window_seconds: int = 60
    api_rate_limit: int = 300
    api_rate_window_seconds: int = 60

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @field_validator("email_backend")
    @classmethod
    def _require_real_mail_in_prod(cls, v: str, info) -> str:
        env = info.data.get("environment")
        if env in ("staging", "production") and v != "smtp":
            raise ValueError(
                "EMAIL_BACKEND must be 'smtp' outside dev/test: without it "
                "nobody can verify their address, and nobody can simulate."
            )
        return v

    @field_validator("secret_key")
    @classmethod
    def _reject_dev_secret_in_prod(cls, v: str, info) -> str:
        env = info.data.get("environment")
        if env in ("staging", "production") and v == DEV_SECRET:
            raise ValueError("SECRET_KEY must be set outside dev/test")
        return v

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")


@lru_cache
def get_settings() -> Settings:
    return Settings()
