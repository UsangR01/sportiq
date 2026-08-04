from functools import lru_cache
from pathlib import Path

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

    # Where trained model artefacts live. models_registry stores a FILENAME, not a path, so the
    # same registry row works on a dev laptop and in a Linux container — promotion stays a DB
    # update rather than a redeploy (TDD §3.1), which absolute paths quietly broke: every
    # registered model pointed at C:\Users\... and could not have loaded in a container at all.
    # Empty means "the repo's own ml/artifacts", which is what local dev wants; a deployment
    # sets MODELS_DIR to wherever the artefacts are mounted or baked in.
    models_dir: str = ""

    @property
    def models_path(self) -> Path:
        if self.models_dir:
            return Path(self.models_dir)
        return Path(__file__).resolve().parents[3] / "ml" / "artifacts"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
