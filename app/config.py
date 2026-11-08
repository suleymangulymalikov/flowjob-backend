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
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"  # cost-efficient, supports Structured Outputs
    openai_timeout_seconds: float = 30.0
    openai_max_output_tokens: int = 1500
    # Prices (USD per 1M tokens) used only to estimate cost from reported usage.
    openai_input_price_per_1m: float = 0.15
    openai_output_price_per_1m: float = 0.60

    # Cost/abuse controls
    match_daily_quota: int = 20  # analyses per user per UTC day
    match_job_description_min_chars: int = 80
    match_job_description_max_chars: int = 12000
    match_cache_ttl_hours: int = 720  # reuse an identical analysis for 30 days

    # CV tailoring (paid, separate per-user cap) + a global daily cap across all AI generations.
    tailor_daily_quota: int = 10
    tailor_cache_ttl_hours: int = 720
    ai_global_daily_quota: int = 500


settings = Settings()
