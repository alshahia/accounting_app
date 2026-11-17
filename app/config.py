"""Deployment configuration loaded from the environment.

Only deployment concerns live here: hosts, secrets, paths, feature flags and
their defaults. Business values (rates, accounts, numbering, limits, labels)
must come from the settings service or reference data, never from this file
(report 21 §3.1, rule R-18.4).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_JWT_SECRET = "dev-only-insecure-secret-change-me"  # noqa: S105 - development-only default
MIN_PRODUCTION_SECRET_BYTES = 32


class Settings(BaseSettings):
    """Settings for one deployment of the API."""

    model_config = SettingsConfigDict(
        env_prefix="MAHMOUD_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Al-Mahmoud ERP"
    version: str = "0.2.0"
    environment: Literal["development", "test", "production"] = "development"
    api_prefix: str = "/api/v1"
    log_level: str = "INFO"
    database_url: SecretStr = SecretStr("postgresql+asyncpg://postgres@127.0.0.1:55432/almahmoud")
    # Every write transaction drops to this non-owner role after binding the
    # tenant, so row-level security applies to the request path too. Set it to
    # the empty string only for a deployment whose database user cannot SET ROLE
    # (for example a managed service that forbids it); isolation then rests on
    # the service filters alone.
    database_set_role: str = "almahmoud_app"
    docs_enabled: bool | None = None

    # Authentication (report 21 §6). Values are deployment settings, not
    # business data; the secret has a development default that production
    # refuses to start with.
    jwt_secret: SecretStr = SecretStr(DEFAULT_JWT_SECRET)
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = Field(default=15, ge=1, le=120)
    refresh_token_days: int = Field(default=30, ge=1, le=365)
    cookie_secure: bool | None = None
    max_failed_logins: int = Field(default=5, ge=1, le=100)
    lockout_minutes: int = Field(default=15, ge=1, le=1440)
    # Failed sign-in attempts allowed per client address and account per minute
    # before the login endpoint itself answers 429. Successful sign-ins are not
    # counted, so ordinary use never reaches it.
    auth_rate_limit_per_minute: int = Field(default=20, ge=1, le=10000)

    # Provisioning (report 21 §3.2, T-29). The fresh-install setup endpoint is
    # gated by an operator-held token; a production process refuses to start
    # without a real one so an unprotected setup screen cannot be shipped.
    bootstrap_token: SecretStr | None = None
    # Privileged DSN used only by the setup endpoint, because a fresh database
    # has no tenant for the forced row-level-security policies to bind to. It
    # defaults to the application DSN, which the supported single-node install
    # runs as the table owner.
    provision_database_url: SecretStr | None = None
    # Invitation and password-reset links are single-use signed tokens; this is
    # how long one stays valid before the administrator must issue another.
    credential_token_hours: int = Field(default=48, ge=1, le=168)

    @model_validator(mode="after")
    def _reject_weak_secret_in_production(self) -> Self:
        if self.environment != "production":
            return self
        secret = self.jwt_secret.get_secret_value()
        if secret == DEFAULT_JWT_SECRET:
            raise ValueError("MAHMOUD_JWT_SECRET must be set in production")
        if len(secret.encode("utf-8")) < MIN_PRODUCTION_SECRET_BYTES:
            raise ValueError(
                "MAHMOUD_JWT_SECRET must be at least "
                f"{MIN_PRODUCTION_SECRET_BYTES} bytes in production"
            )
        bootstrap = self.bootstrap_token.get_secret_value() if self.bootstrap_token else ""
        if len(bootstrap.encode("utf-8")) < MIN_PRODUCTION_SECRET_BYTES:
            raise ValueError(
                "MAHMOUD_BOOTSTRAP_TOKEN must be at least "
                f"{MIN_PRODUCTION_SECRET_BYTES} bytes in production"
            )
        return self

    @property
    def provisioning_url(self) -> str:
        """The DSN the setup endpoint opens; the application DSN by default."""
        if self.provision_database_url is not None:
            return self.provision_database_url.get_secret_value()
        return self.database_url.get_secret_value()

    @property
    def docs_are_enabled(self) -> bool:
        """Expose OpenAPI/docs only outside production unless set explicitly."""
        if self.docs_enabled is not None:
            return self.docs_enabled
        return self.environment != "production"

    @property
    def cookies_are_secure(self) -> bool:
        """Secure cookies everywhere except local HTTP development and tests."""
        if self.cookie_secure is not None:
            return self.cookie_secure
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
