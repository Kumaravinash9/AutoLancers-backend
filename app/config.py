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

    # Which LLM drafts proposals and judges skill matches: "gemini" or "anthropic".
    llm_provider: str = "gemini"

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-4-8"

    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.6-flash"

    # Semantic skill matching (see app/services/matching.py). The pipeline only pays for it on
    # postings that pass the hard filters and aren't already cached, and never more than
    # ``skill_match_max_per_cycle`` fresh matches per cycle — so widening the skill filter can't
    # turn discovery volume straight into LLM spend. Anything skipped, deferred, or failed falls
    # back to substring matching.
    skill_match_enabled: bool = True

    # Ceiling on *new* LLM matches computed in a single cycle. Cache hits and hard-rejected jobs
    # don't draw from it; jobs past the ceiling fall back to substring this cycle and get matched
    # on a later one (the cache persists once computed). <= 0 means no ceiling (unlimited).
    skill_match_max_per_cycle: int = 25

    # Discovery-skill expansion (see app/services/skill_expansion.py). Broadens the profile's
    # confirmed skills into the wider marketplace tag vocabulary a client might tag a matching job
    # with ('React' -> also 'Web Development', 'JavaScript'), so generically-tagged jobs surface in
    # the server-side search. Recomputed only when the profile's skills change, so it's a
    # per-edit cost, not per-cycle. Off falls back to searching on the listed skills alone.
    skill_expansion_enabled: bool = True

    # Master switch for placing real bids. Off by default and checked on every submission, so
    # bidding cannot be reached by a stray request, a bad merge, or a UI bug — only by a
    # deliberate config change plus a per-job confirmation.
    enable_bidding: bool = False

    frontend_origin: str = "http://localhost:3000"
    # Every 30 minutes. Freelancer's terms ask for cached data to be refreshed at least
    # daily, and a tighter loop mostly re-reads postings that haven't changed.
    poll_interval_seconds: int = 1800
    scheduler_enabled: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
