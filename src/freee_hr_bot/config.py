from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from freee_hr_bot.proposals.validate import to_minutes


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
    # Break filled in when a work record request does not mention breaks
    default_break_start: str = "12:00"
    default_break_end: str = "13:00"

    @model_validator(mode="after")
    def _check_default_break(self) -> "Settings":
        if to_minutes(self.default_break_end) <= to_minutes(self.default_break_start):
            raise ValueError("DEFAULT_BREAK_END must be after DEFAULT_BREAK_START")
        return self

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
