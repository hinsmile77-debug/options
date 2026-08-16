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
    # 2026-08-16 (Block C) — **체결통보 WS 구독의 `tr_key`가 이 값이다.**
    #
    # 공식 문서("선물옵션 실시간체결통보" 시트)의 Request Body 표는 tr_key를 *"예:101S12"*
    # (종목코드)로 적었지만, 같은 시트의 Response Example은 `"tr_key": "HTS ID"`를 돌려준다 —
    # 계좌별 통보라 종목이 아니라 **사용자를 특정**하는 것이 맞다. 문서 두 곳이 어긋나 있으므로
    # 8/18 실측에서 어느 쪽인지 확정한다(구독 ACK의 `rt_cd`가 답을 준다).
    #
    # 비어 있으면 체결통보를 **구독하지 않는다**(예외를 던지지 않는다) — 이 값이 없다고 관측
    # 루프가 안 뜨면 안 되고, 조용히 안 걸리는 것도 안 된다. `order_notice.subscription()`이
    # None을 돌려주고 호출측이 경고를 남긴다.
    kis_hts_id: str = Field(default="", alias="KIS_HTS_ID")

    @property
    def is_mock(self) -> bool:
        return self.kis_env.lower() != "prod"


class SlackSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(PROJECT_ROOT / ".env"), extra="ignore")

    slack_bot_token: str = Field(default="", alias="SLACK_BOT_TOKEN")
    slack_channel_id: str = Field(default="", alias="SLACK_CHANNEL_ID")
    # DB(slack_alert_settings)에 아직 아무도 토글한 적 없을 때(최초 기동)의 기본값 —
    # 2026-07-19 운영점검보고서 §5-4, mahdi/data/db.py의 is_slack_alerts_enabled() 참고.
    slack_alerts_enabled_default: bool = Field(default=True, alias="SLACK_ALERTS_ENABLED_DEFAULT")

    @property
    def is_configured(self) -> bool:
        return bool(self.slack_bot_token and self.slack_channel_id)


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
def get_slack_settings() -> SlackSettings:
    return SlackSettings()


@lru_cache
def get_risk_limits() -> dict[str, Any]:
    return _load_yaml("risk_limits.yaml")


@lru_cache
def get_strategy_params() -> dict[str, Any]:
    return _load_yaml("strategy_params.yaml")


@lru_cache
def get_event_calendar() -> dict[str, Any]:
    """
    계산: `event_calendar.yaml`(수기 매크로 이벤트 캘린더)을 읽는다 — 2026-08-05 신규.
    해석: 다른 설정과 같이 `@lru_cache`라 **프로세스당 1회만** 읽는다. 파일을 고쳐도 관측 루프를
         재시작해야 반영된다(장전 07:30 기동에 자연히 반영). 파일이 없으면 빈 dict를 돌려주고
         호출측(`fusion.event_calendar`)이 `status="empty"`로 경고한다 — **여기서 예외를 던지면
         캘린더 하나 때문에 관측 루프 전체가 못 뜬다.**
    """
    try:
        return _load_yaml("event_calendar.yaml") or {}
    except FileNotFoundError:
        return {}
