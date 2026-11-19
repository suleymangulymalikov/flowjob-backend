from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    mongodb_uri: str
    mongodb_db_name: str = "flowjob"

    cookie_secure: bool = False  # Set true behind HTTPS in production

    jwt_secret: str
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 30

    # --- OpenAI job matching (paid). No key => matching returns 503, no calls are made. ---
    # OPENAI_API_KEY=mock returns canned AI results with no network call. It is for automated tests and local
    # development only, so it also needs ALLOW_MOCK_AI=true and is refused with production settings (see check_mock_ai).
    openai_api_key: str | None = None
    allow_mock_ai: bool = False
    # Measured on scripts/tailor_eval: gpt-4.1 counts years of experience correctly, keeps soft traits out of the score
    # and writes rewrites that pass the truthfulness checks; gpt-4o-mini and gpt-4.1-mini did not.
    openai_model: str = "gpt-4.1"
    openai_timeout_seconds: float = 30.0
    openai_max_output_tokens: int = 1500
    # Prices (USD per 1M tokens) used only to estimate cost from reported usage.
    openai_input_price_per_1m: float = 2.00
    openai_output_price_per_1m: float = 8.00

    # Cost/abuse controls
    match_daily_quota: int = 20  # analyses per user per UTC day
    match_job_description_min_chars: int = 80
    match_job_description_max_chars: int = 12000
    match_cache_ttl_hours: int = 720  # reuse an identical analysis for 30 days
    # Each requirement carries a quote, citations and an explanation (~95 output tokens): up to 25 of them need more than
    # the shared default. Overflow fails the whole request (and refunds the quota).
    match_max_output_tokens: int = 3000
    match_timeout_seconds: float = 60.0
    match_temperature: float | None = 0.0  # None disables it; models that reject it are retried without it

    # CV tailoring (paid, separate per-user cap) + a global daily cap across all AI generations.
    tailor_daily_quota: int = 10
    tailor_cache_ttl_hours: int = 720
    # Tailoring proposes up to 12 typed changes with reasons and evidence: larger budget than the shared default.
    tailor_max_output_tokens: int = 3500
    tailor_timeout_seconds: float = 60.0
    tailor_temperature: float | None = 0.2
    ai_global_daily_quota: int = 500

    # --- Gmail integration (optional). Without the Google client and token key, Gmail reports itself unavailable
    # and every other feature works unchanged. ---
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: str | None = None
    # Must match the Google Cloud OAuth client exactly. The default goes through the Vite dev proxy so the
    # browser-binding cookie stays same-origin with the frontend.
    google_oauth_redirect_uri: str = "http://localhost:5173/api/v1/gmail/oauth/callback"
    # Base64 (urlsafe or standard) 32-byte AES-256-GCM key for stored refresh tokens. Separate from jwt_secret.
    gmail_token_key: str | None = None
    # Comma-separated previous keys, accepted for decryption only (rotation).
    gmail_token_keys_old: str = ""
    frontend_base_url: str = "http://localhost:5173"
    gmail_scheduler_enabled: bool = False  # off by default: tests and imports never start background work
    gmail_sync_interval_hours: float = 3.0
    gmail_scheduler_tick_seconds: float = 600.0
    gmail_backfill_days: int = Field(default=30, ge=1, le=30)
    gmail_overlap_days: int = Field(default=2, ge=0, le=7)
    gmail_batch_size: int = Field(default=50, ge=1, le=50)
    gmail_max_batches_per_run: int = Field(default=4, ge=1, le=4)
    gmail_max_list_pages_per_run: int = Field(default=10, ge=1, le=50)
    gmail_manual_sync_min_interval_seconds: int = 60
    gmail_lock_ttl_seconds: int = 600
    gmail_daily_quota: int = 200  # AI classifications per user per UTC day
    gmail_global_daily_quota: int = 2000  # separate from ai_global_daily_quota (Match/Tailor)
    gmail_max_output_tokens: int = 2000
    gmail_timeout_seconds: float = 60.0

    @property
    def gmail_configured(self) -> bool:
        return bool(self.google_oauth_client_id and self.google_oauth_client_secret and self.gmail_token_key)

    @property
    def mock_ai(self) -> bool:
        """True when AI features return canned results instead of calling OpenAI."""
        return self.openai_api_key == 'mock'


class MockAiNotAllowed(RuntimeError):
    """OPENAI_API_KEY=mock is configured where canned AI results must never reach users."""


def mock_ai_problem(config: Settings) -> str | None:
    """Why mock AI may not run with these settings, or None when it is off or explicitly allowed.

    Mock mode returns invented match scores and tailoring suggestions, so it is opt-in twice: the key must be the
    literal `mock` AND ALLOW_MOCK_AI must be true. COOKIE_SECURE=true marks an HTTPS (production) deployment, where
    mock mode is refused even if it was allowed. The message never includes configuration values.
    """
    if not config.mock_ai:
        return None
    if not config.allow_mock_ai:
        return ('OPENAI_API_KEY=mock returns canned AI results and is only for automated tests and local development. '
                'Set a real OPENAI_API_KEY, leave it empty to disable AI features, or set ALLOW_MOCK_AI=true locally.')
    if config.cookie_secure:
        return 'OPENAI_API_KEY=mock cannot be used with COOKIE_SECURE=true, which marks a production deployment.'
    return None


def check_mock_ai(config: Settings) -> None:
    if problem := mock_ai_problem(config):
        raise MockAiNotAllowed(problem)


settings = Settings()
check_mock_ai(settings)  # Refuse to start rather than serve invented AI results.
