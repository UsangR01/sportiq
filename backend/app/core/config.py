from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Every scheme a managed Postgres might hand us, mapped to the async driver this project
# actually has installed. Render's `fromDatabase: connectionString` yields "postgresql://...",
# Heroku-style providers still emit the legacy "postgres://", and neither names a driver.
#
# WHY THIS IS NOT COSMETIC: requirements.txt carries asyncpg and NO sync driver, so SQLAlchemy
# resolves a bare "postgresql://" to its default psycopg2 dialect and fails with "The asyncio
# extension requires an async driver to be used" -- at which point alembic upgrade head dies in
# Render's preDeployCommand and the web service never starts. It killed the first real deploy.
#
# Rewriting here rather than asking the operator to hand-set DATABASE_URL is deliberate: that
# value is auto-wired from the database resource in the blueprint, so overriding it by hand
# would mean giving up the wiring (and re-pasting a password by hand on every credential
# rotation).
_ASYNC_POSTGRES_SCHEME = "postgresql+asyncpg://"
_BARE_POSTGRES_SCHEMES = ("postgresql://", "postgres://")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://sportiq_user:password@localhost:5432/sportiq"

    @field_validator("database_url")
    @classmethod
    def _require_an_async_driver(cls, value: str) -> str:
        """Normalise a driverless Postgres URL onto asyncpg. See _BARE_POSTGRES_SCHEMES.

        A URL that already names a driver is left exactly as it is -- including a deliberate
        sync one, which a future script might legitimately want. Only the ambiguous case is
        rewritten."""
        for scheme in _BARE_POSTGRES_SCHEMES:
            if value.startswith(scheme):
                return _ASYNC_POSTGRES_SCHEME + value[len(scheme) :]
        return value

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
    # Second corner-statistics source behind API-Football, and the ONLY one for
    # Veikkausliiga (API-Football has 0% there). Previously read from keys.docx by an
    # offline collector, so a deployed worker never had it -- see infra/render.yaml.
    thestatsapi_key: str = ""

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
