from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://sportiq_user:password@localhost:5432/sportiq"
    redis_url: str = "redis://localhost:6379/0"

    jwt_secret_key: str = ""
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7

    # Data source adapters (TDD §2.2) — empty by default; adapters must no-op /
    # fall back gracefully when their key is absent, never raise on missing config.
    therundown_api_key: str = ""
    api_football_key: str = ""
    balldontlie_api_key: str = ""
    rotowire_api_key: str = ""
    highlightly_api_key: str = ""

    expo_access_token: str = ""

    cors_origins: str = ""

    sentry_dsn_backend: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
