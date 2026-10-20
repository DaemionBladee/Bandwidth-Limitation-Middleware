from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql+psycopg://bandwidth:bandwidth@127.0.0.1:5432/bandwidth"
    cookie_secure: bool = True
    session_hours: int = Field(default=8, ge=1, le=24)
    admin_api_token: str = ""
    accounting_source: str = "mock"
    accounting_file: str = ""
    ise_mode: str = "mock"
    ise_base_url: str = ""
    ise_username: str = ""
    ise_password: str = ""
    ise_ca_bundle: str = ""
    ise_contract_file: str = ""
    ise_timeout_seconds: float = Field(default=5, gt=0, le=30)
    worker_interval_seconds: int = Field(default=5, ge=1, le=3600)
    enforcement_max_attempts: int = Field(default=5, ge=1, le=10)


@lru_cache
def settings() -> Settings:
    return Settings()
