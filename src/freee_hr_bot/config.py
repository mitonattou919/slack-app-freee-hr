from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    slack_bot_token: str
    slack_app_token: str

    freee_client_id: str
    freee_client_secret: str
    freee_company_id: int

    gemini_api_key: str
    gemini_model: str = "gemini-2.5-flash"

    # Fernet key used to encrypt freee tokens at rest (generate with `make key`)
    fernet_key: str

    db_path: str = "./data/bot.db"
    tz: str = "Asia/Tokyo"
    proposal_ttl_minutes: int = 15
    max_work_record_days: int = Field(default=7, ge=1)

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
