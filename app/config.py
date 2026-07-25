from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/automatelancers"

    freelancer_client_id: str = ""
    freelancer_client_secret: str = ""
    freelancer_redirect_uri: str = "http://localhost:8000/auth/freelancer/callback"

    token_encryption_key: str = ""

    anthropic_api_key: str = ""

    frontend_origin: str = "http://localhost:3000"
    poll_interval_seconds: int = 25
    scheduler_enabled: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
