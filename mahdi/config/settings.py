"""프로젝트 전역 설정 — .env(비밀값) + YAML(전략/리스크 파라미터)을 단일 지점에서 로드한다."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

CONFIG_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CONFIG_DIR.parent.parent


class KISSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(PROJECT_ROOT / ".env"), extra="ignore")

    kis_app_key: str = Field(default="", alias="KIS_APP_KEY")
    kis_app_secret: str = Field(default="", alias="KIS_APP_SECRET")
    kis_account_no: str = Field(default="", alias="KIS_ACCOUNT_NO")
    kis_account_product_code: str = Field(default="01", alias="KIS_ACCOUNT_PRODUCT_CODE")
    kis_env: str = Field(default="vps", alias="KIS_ENV")  # vps=모의투자, prod=실전

    @property
    def is_mock(self) -> bool:
        return self.kis_env.lower() != "prod"


class DBSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(PROJECT_ROOT / ".env"), extra="ignore")

    db_host: str = Field(default="localhost", alias="DB_HOST")
    db_port: int = Field(default=5432, alias="DB_PORT")
    db_name: str = Field(default="mahdi", alias="DB_NAME")
    db_user: str = Field(default="mahdi", alias="DB_USER")
    db_password: str = Field(default="mahdi", alias="DB_PASSWORD")

    redis_host: str = Field(default="localhost", alias="REDIS_HOST")
    redis_port: int = Field(default=6379, alias="REDIS_PORT")

    @property
    def dsn(self) -> str:
        return (
            f"postgresql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


def _load_yaml(name: str) -> dict[str, Any]:
    with open(CONFIG_DIR / name, encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache
def get_kis_settings() -> KISSettings:
    return KISSettings()


@lru_cache
def get_db_settings() -> DBSettings:
    return DBSettings()


@lru_cache
def get_risk_limits() -> dict[str, Any]:
    return _load_yaml("risk_limits.yaml")


@lru_cache
def get_strategy_params() -> dict[str, Any]:
    return _load_yaml("strategy_params.yaml")
