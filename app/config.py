from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/automatelancers"

    freelancer_client_id: str = ""
    freelancer_client_secret: str = ""
    freelancer_redirect_uri: str = "http://localhost:8000/auth/freelancer/callback"

    token_encryption_key: str = ""

    # Signs session cookies. Changing it invalidates every session, which is the intended
    # emergency lever.
    jwt_secret: str = ""
    session_ttl_hours: int = 24 * 14
    # Cookies are only marked Secure over HTTPS; leave false for local http development.
    cookie_secure: bool = False

    # Which LLM drafts proposals: "gemini" or "anthropic".
    llm_provider: str = "gemini"

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5"

    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.6-flash"

    # Master switch for placing real bids. Off by default and checked on every submission, so
    # bidding cannot be reached by a stray request, a bad merge, or a UI bug — only by a
    # deliberate config change plus a per-job confirmation.
    enable_bidding: bool = False

    frontend_origin: str = "http://localhost:3000"
    poll_interval_seconds: int = 25
    scheduler_enabled: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
