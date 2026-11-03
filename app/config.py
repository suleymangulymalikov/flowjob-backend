from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    mongodb_uri: str
    mongodb_db_name: str = "flowjob"

    jwt_secret: str
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 30


settings = Settings()
