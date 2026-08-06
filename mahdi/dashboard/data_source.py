"""COCKPIT 데이터 소스 — DB 우선 조회, 실패/데이터 없음 시 합성 리플레이로 폴백.

폴백이 있는 이유: 대시보드는 실시간 수집 파이프라인이 아직 안 돌고 있어도(또는 장 종료 후에도)
독립 실행 가능해야 관측 인프라 검증에 쓸모가 있다.
"""

from __future__ import annotations

import contextlib
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta

import numpy as np

from mahdi.config.settings import PROJECT_ROOT
from mahdi.data import db
from mahdi.engines.regime import RegimeLabel
from mahdi.engines.regime_pipeline import FEATURE_VERSION, MACRO_SNAPSHOT_MAX_AGE_MINUTES
from mahdi.execution.account_tracker import BalanceSnapshot, build_account_state
from mahdi.features.options_intel import find_gamma_flip, gamma_walls as compute_gamma_walls, signal_book_legs
from mahdi.ops import db_metrics
from mahdi import session

logger = logging.getLogger("mahdi.dashboard.data_source")

# 2026-07-22(운영점검보고서 §1-1) — 관측 루프(mahdi.main.LAST_START_MARKER_FILE)와 대칭인
# COCKPIT 전용 마커. 07-21 08:15에 뜬 COCKPIT이 그날 15:45 종료 실패 이후에도 밤새 좀비로
# 남아 07-22 07:30까지 이어졌는데, 이 사실을 알아채려면 cockpit.log의 Uvicorn 배너 유무를
# 근거로 역추적해야 했다 — 이 프로세스가 "언제 새로 떴는지"를 자체 기록해두면 그럴 필요가 없다.
COCKPIT_START_MARKER_FILE = PROJECT_ROOT / "logs" / ".last_cockpit_start.txt"

# 심볼 혼입 버그(2026-07-06) 시기에 쓰던 옛 고정 라벨 — 더 이상 아무도 안 쓰지만 남아있는
# 화석 데이터라 Flow Radar "가장 활발한 종목" 선정에서 제외한다.
_LEGACY_MIXED_SYMBOL = "KOSPI200_OPT"

# Flow Radar "가장 활발한 옵션" 선정 룩백 윈도 — 2026-07-06 위클리 북 추가 후 실측: 여러 위클리
# 종목이 같은 1분봉 timestamp로 동시에 찍혀("ORDER BY max(timestamp) DESC"에 동률), COCKPIT이
# 10초마다 리런될 때마다 임의로 다른 종목이 뽑혀 차트가 매번 완전히 다른 종목(다른 가격대)으로
# 바뀌어 보이는 문제가 실제로 발생함. "가장 최근 틱 1개"가 아니라 "최근 N분간 누적거래량"으로
# 기준을 바꿔, 단일 틱의 우연한 타이밍이 아니라 실제 상대적 활발함이 선정을 좌우하게 한다.
FLOW_RADAR_OPTION_LOOKBACK_MINUTES = 10

# 2026-08-05(COCKPIT 육안 점검 P2-9) — Flow Radar가 그리는 시간 창.
#
# 종전에는 두 계열 모두 `LIMIT 60`(**행** 60개)이었다. 선물은 거의 매분 체결돼 60행 ≈ 60분이라
# 의도와 우연히 일치했지만, **옵션은 거래가 뜸해 60행이 몇 시간에 걸친다.** 그런데 app.py가
# 옵션 x축을 선물 창(약 60분)으로 강제하므로(거래가 1~2점뿐일 때 x축이 마이크로초로 깨지는
# 2026-07-06 문제 대응) 창 밖 점들은 **안 보이는데 y축 자동범위에는 그대로 들어간다.**
#
# 08-05 화면 실측이 정확히 그 상태였다: 옵션 OFI 축이 −30까지 내려가 있는데 보이는 데이터는
# −6~+8이었고, 가격 축은 26까지인데 보이는 최대는 21이었다 — 실제 변동이 축 아래쪽에 눌려
# 사실상 평평하게 보였다. 조회를 시간 기준으로 바꾸면 창 밖 점이 **애초에 오지 않으므로**
# 이 왜곡이 원인에서 사라진다(Plotly 축 설정으로 덮는 것보다 근본적이다).
#
# 60분인 이유: 종전 선물 계열의 실효 창(60행 ≈ 60분)과 같게 둬 화면의 시간 폭을 바꾸지 않는다 —
# 이번 변경의 목적은 창을 바꾸는 게 아니라 **두 계열이 같은 창을 보게** 하는 것이다.
FLOW_RADAR_WINDOW_MINUTES = 60
# 시간 창 안에서도 행 수 상한은 유지한다 — 1분봉이라 정상적으로는 60행을 넘을 수 없지만,
# 재처리/중복 적재 등으로 창 안 행이 폭증하면 대시보드가 그것을 그리다 멈추면 안 된다.
FLOW_RADAR_ROW_CAP = 240


@dataclass(frozen=True, slots=True)
class ChainPoint:
    strike: float
    gex: float


@dataclass
class DashboardSnapshot:
    as_of: datetime
    is_live: bool  # DB에서 가져왔으면 True, 합성 폴백이면 False
    regime: RegimeLabel
    regime_prob: dict[RegimeLabel, float]
    higher_tf_regime: RegimeLabel | None
    stability_flag: bool
    # 2026-08-05(COCKPIT 육안 점검 P1-7, 마이그레이션 025) — `regime_prob`가 학습된 사후확률인가,
    # `warmup_fallback()`의 one-hot 상수인가. True면 확률 막대를 그리면 안 된다(8개 중 하나가
    # 100%인 그림은 "확신"으로 읽히는데 실제로는 "확률을 계산한 적이 없다"는 뜻이다).
    # None = 마이그레이션 025 이전에 적재된 행이라 둘 중 무엇인지 알 수 없음.
    regime_is_warmup: bool | None
    spot: float
    # 2026-08-05(COCKPIT 육안 점검 P1-6) — 위 `spot`을 **언제 관측한 값인가**. 화면에 시각 없이
    # 숫자만 띄우면 장전 전일 종가와 장중 실시간 지수가 구분되지 않고, 같은 화면의 선물 체결가와
    # 벌어져 있어도 어느 쪽이 낡은 것인지 알 수 없다(08-05 실측: 지수 1,042.85 vs 선물 1046대).
    # 합성 폴백에서는 None.
    spot_asof: datetime | None
    # chain/gamma_flip/gamma_walls는 전부 **`gex_expiry` 한 북**에서만 나온다 — 세 북(먼슬리 +
    # 위클리 월·목)을 합산하면 만기별 정보가 서로를 덮기 때문(2026-08-05 P0-2, 아래 `gex_expiry`
    # 주석 참고). 화면에 어느 북인지 반드시 함께 표시해야 하므로 만기를 스냅샷에 싣는다.
    chain: list[ChainPoint]
    gamma_flip: float | None
    gamma_walls: list[float]
    # 2026-08-05(COCKPIT 육안 점검 P0-2) — 위 세 값이 실제로 어느 북에서 나왔는지. 관측 루프가
    # `signal_decisions.gex_expiry`(마이그레이션 023)에 같은 값을 남기므로, 화면의 만기와 판단
    # 이력의 만기를 대조하면 "화면과 판단이 같은 체인을 보고 있는가"를 사람이 확인할 수 있다.
    # 체인이 비었으면 None.
    gex_expiry: date | None
    # Flow Radar는 선물(기초자산)과 옵션(가장 활발한 종목) 두 계열을 따로 보여준다 — 선물은
    # WS 구독이 항상 켜져 있어 거의 매분 체결되므로, "가장 최근 활동"만으로 대표 종목을 뽑으면
    # 옵션이 영원히 안 뽑힌다(2026-07-06 사용자 지적으로 분리). VPIN은 종목 구분 없이 둘 다 계산된다.
    futures_flow_symbol: str | None
    timestamps: list[datetime]
    ofi_series: list[float]
    vpin_series: list[float]
    price_series: list[float]
    microprice_series: list[float]
    option_flow_symbol: str | None
    option_timestamps: list[datetime]
    option_ofi_series: list[float]
    option_vpin_series: list[float]
    option_price_series: list[float]
    option_microprice_series: list[float]
    foreign_net: float
    institution_net: float
    individual_net: float
    # Phase 1.5-④(2026-07-06 추가, 2026-07-10 위클리를 월/목 두 상품으로 분리) — 먼슬리/위클리(월)/
    # 위클리(목) 북별 ATM±2 유동성 스냅샷(북당 최신 1건). series 값: "regular"|"weekly_mon"|"weekly_thu".
    # 각 dict 키: series, expiry, atm_spread_pct, depth, volume, days_to_expiry.
    expiry_liquidity: list[dict]
    # Cross-asset stress(v6 §7.3, 2026-07-10 신규) — db.latest_macro_snapshot() 반환 형태 그대로:
    # {vix_front, vix_next, vix_term_structure, usdcnh, us10y_yield} 또는 폴링이 아직 안 돌았으면 None.
    macro_snapshot: dict | None


def get_slack_alerts_enabled() -> bool:
    """
    계산: slack_alert_settings(2026-07-19 §5-4)를 조회한다 — mahdi.main(관측 루프, COCKPIT과는
         별도 프로세스)과 On/Off 값을 공유하는 단일 진실 공급원(SSOT)이 DB이기 때문에 여기서도
         DB를 직접 조회한다(메모리 전역변수로는 프로세스 간 공유가 안 됨).
    실패 조건: DB 연결 실패 시 True(알림 켜짐)로 보수적으로 폴백 — COCKPIT이 "알림이 꺼져있다"고
              잘못 표시해 사용자가 안심하는 것보다는, 실제로는 켜져 있는데 꺼진 것처럼 보이는 게
              더 안전한 방향(전자는 사용자가 알림이 온다고 착각하게 만듦)이라 이 방향으로 정함.
    """
    try:
        with db.get_connection() as conn:
            return db.is_slack_alerts_enabled(conn)
    except Exception:
        logger.warning("슬랙 알림 설정 조회 실패", exc_info=True)
        return True


def set_slack_alerts_enabled(enabled: bool) -> None:
    """계산: COCKPIT 체크박스 토글 → DB 저장. 저장 즉시 mahdi.main의 다음 notify() 호출부터
    반영된다(재시작 불필요) — 실패해도 COCKPIT 자체가 죽으면 안 되므로 예외를 삼키고 로그만 남긴다."""
    try:
        with db.get_connection() as conn:
            db.set_slack_alerts_enabled(conn, enabled)
    except Exception:
        logger.warning("슬랙 알림 설정 저장 실패", exc_info=True)


@dataclass(frozen=True, slots=True)
class HealthCheck:
    label: str
    status: str  # "ok" | "warning" | "info"
    detail: str
    # 2026-08-01(§5-5): 배지가 11 → 15개가 되면서 한 줄에 다 펴면 각 열이 너무 좁아진다.
    # app.py가 이 값으로 묶어 2행으로 렌더링한다. 기본값은 기존 배지와의 하위호환.
    group: str = "인프라"


# 2026-07-19(§5-6 "오늘의 점검 요약") — 1-B 장중 체크리스트의 "결손 여부" 기준(§5-4 Slack 알림의
# OPTION_CHAIN_GAP_ALERT_SECONDS와 동일한 5분)과 정규장 시간. 공휴일 캘린더는 없음(이 코드베이스
# 어디에도 아직 없음 — 평일 09:00~15:45만 "장중"으로 본다).
_TRADING_DAY_START = dtime(9, 0)
_TRADING_DAY_END = dtime(15, 45)
_STALE_DATA_THRESHOLD_SECONDS = 300.0

# 2026-08-03(COCKPIT 육안 점검) — KOSPI200 선물·옵션 **장 마감 동시호가(종가 단일가)** 시작 시각.
#
# 이 구간(15:35~15:45)에는 연속 체결이 없어 WS 체결 메시지가 끊기고 1분봉도 안 만들어진다.
# 그런데 `_is_trading_hours()`는 15:45까지 True이고 결손 임계는 5분이라, **매 거래일 15:40부터
# 15:45까지 "선물 시세 N분째 결손" 노란불이 반드시 뜬다.** 08-03 15:44:53 화면이 정확히 그
# 순간을 잡았다("12분째 결손", WS 마지막 수신 15:34:55).
#
# 이것은 2026-07-31 §2-2에서 CB 하트비트로 배운 것과 같은 실수다 — **정상을 이상으로 표시하면
# 진짜 이상을 못 알아본다.** 단일가 구간에서는 결손 나이를 `now`가 아니라 이 시각 기준으로 재서,
# "단일가 진입 전부터 이미 끊겨 있었나"만 판정한다(14:00에 멈춘 경우는 여전히 경고).
#
# 2026-08-05(§2-8): 이 상수는 **`mahdi.session`으로 옮겼다.** 08-05에 같은 사실이 필요한 곳이
# 하나 더 나왔는데(판단 경로의 `orderflow_ofi_vpin` 미가용 사유) 그때까지 이 지식은 화면에만
# 있었다 — 규약 B(같은 것은 한 곳에서)의 적용이다. 이름은 하위 호환을 위해 유지한다.
_CLOSING_AUCTION_START = session.CLOSING_AUCTION_START


def _is_trading_hours(now: datetime) -> bool:
    return now.weekday() < 5 and _TRADING_DAY_START <= now.time() <= _TRADING_DAY_END


def _as_naive(ts: datetime) -> datetime:
    """TIMESTAMPTZ 컬럼에서 읽은 tz-aware 값을 db.local_now()와 같은 좌표계(naive KST)로 맞춘다.

    2026-07-20 `_freshness_check`, 2026-07-31 `_market_halt_check`에서 각각 같은 유형의
    TypeError를 실측했다 — 저장 정책상 라벨만 UTC일 뿐 값은 이미 KST 벽시계라 변환이 아니라
    tzinfo 제거가 맞다(db.local_now() docstring 참고).
    """
    return ts.replace(tzinfo=None) if ts.tzinfo is not None else ts


def _freshness_check(
    label: str, latest_ts: datetime | None, now: datetime, *, continuous_trading_only: bool = False
) -> HealthCheck:
    """장중이 아니면(주말/장외시간) 데이터가 안 들어와도 정상이므로 판단하지 않는다 — 장중에만
    §5-4와 동일한 5분 기준으로 결손 여부를 판단한다.

    `continuous_trading_only=True`는 **연속 체결에만 의존하는 데이터**(WS 체결 기반)에 쓴다 —
    종가 단일가 구간(15:35~15:45)에는 체결이 없는 것이 정상이므로 그 구간 시작 시각을 기준으로
    나이를 잰다. REST 폴링 기반(옵션체인)은 단일가 구간에도 계속 들어오므로 False(기본)다.

    2026-07-20(버그 수정): latest_ts는 TIMESTAMPTZ 컬럼(MAX(timestamp))에서 psycopg가 읽어온
    값이라 tzinfo가 붙어 있는데, now(db.local_now())는 naive다 — 장외시간에는 이 함수가 그 값을
    한 번도 안 써서(위 `_is_trading_hours` 조기 반환) 안 드러나다가, 오늘(2026-07-20) 정규장
    시간에 처음 실제로 `now - latest_ts`가 실행되며 TypeError로 전체 헬스체크가 죽는 것을
    실측했다. db.local_now()의 "naive-KST가 세션 타임존(UTC) 라벨로 저장된다"는 정책(그 함수
    docstring 참고) 때문에 tzinfo만 떼면 벽시계 숫자는 이미 같은 좌표계 — 실제 시간대 변환은
    필요 없다.
    """
    if not _is_trading_hours(now):
        return HealthCheck(label, "info", "장중 아님(평일 09:00~15:45 외)")
    if latest_ts is None:
        return HealthCheck(label, "warning", "장중인데 데이터가 아직 한 건도 없음")
    # 2026-08-03: 연속 체결이 없는 구간(종가 단일가)에서는 `now`가 아니라 그 구간 시작 시각을
    # 기준으로 잰다 — 상세 근거는 `_CLOSING_AUCTION_START` 주석 참고.
    reference = now
    if continuous_trading_only and now.time() >= _CLOSING_AUCTION_START:
        reference = datetime.combine(now.date(), _CLOSING_AUCTION_START)
    age_seconds = max((reference - _as_naive(latest_ts)).total_seconds(), 0.0)
    if age_seconds >= _STALE_DATA_THRESHOLD_SECONDS:
        return HealthCheck(label, "warning", f"{age_seconds / 60:.0f}분째 결손")
    return HealthCheck(label, "ok", f"{age_seconds:.0f}초 전 갱신")


def _option_chain_freshness_check(conn, underlying: str, now: datetime) -> HealthCheck:
    label = "옵션체인(option_analysis_1m)"
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(timestamp) FROM option_analysis_1m WHERE underlying=%s", (underlying,))
            row = cur.fetchone()
        latest_ts = row[0] if row else None
    except Exception:
        conn.rollback()
        logger.warning("옵션체인 결손 점검 조회 실패", exc_info=True)
        return HealthCheck(label, "warning", "조회 실패")
    return _freshness_check(label, latest_ts, now)


def _futures_freshness_check(conn, underlying: str, now: datetime) -> HealthCheck:
    # WS가 살아있는지 직접 볼 방법은 COCKPIT(별도 프로세스)에 없으므로, 선물 1분봉이 계속
    # 들어오고 있는지를 대리 지표로 쓴다 — WS가 끊기면(재연결 로직이 있어도 그 사이엔) 선물
    # 체결도 같이 끊긴다.
    label = "선물 시세(market_raw_1m, WS 생존 대리 지표)"
    try:
        futures_symbol = db.get_active_futures_symbol(conn, underlying)
        if futures_symbol is None:
            return HealthCheck(label, "info", "선물 심볼 미등록(관측 루프 미기동)")
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(timestamp) FROM market_raw_1m WHERE symbol=%s", (futures_symbol,))
            row = cur.fetchone()
        latest_ts = row[0] if row else None
    except Exception:
        conn.rollback()
        logger.warning("선물 결손 점검 조회 실패", exc_info=True)
        return HealthCheck(label, "warning", "조회 실패")
    return _freshness_check(label, latest_ts, now, continuous_trading_only=True)


# 2026-07-20 — 옵션체인 콜/풋 조회 성공률 비대칭 발견(NEXT_TODO/DECISION_LOG 참고). 공유
# _RateLimiter(rest_client.py)가 행사가마다 콜→풋 순서로 호출하는데 KIS 모의투자의 실제 한도가
# 설정값보다 빡빡해, 매 쌍의 두 번째 호출(풋)만 계속 500이 되는 패턴이 실측(콜 18~19건 vs 풋
# 3건, 행사가 5개 전부 동일 경향)됐다 — 사이클 전체가 실패하는 경우(§3-1, gap 알림으로 이미
# 커버됨)와 달리 한쪽만 계속 죽는 이 결손은 지금까지 계측된 적이 없었다.
_OPTION_LEG_BALANCE_LOOKBACK_MINUTES = 10
_OPTION_LEG_BALANCE_MIN_RATIO = 0.5  # 적은 쪽/많은 쪽 비율이 이 밑으로 떨어지면 경고


def _option_chain_leg_balance_check(conn, underlying: str, now: datetime) -> HealthCheck:
    """
    계산: 최근 _OPTION_LEG_BALANCE_LOOKBACK_MINUTES분간 option_analysis_1m의 콜/풋 적재 건수를
         비교한다. 콜/풋 중 적은 쪽이 많은 쪽의 절반에도 못 미치면 위 발견 패턴의 재발로 보고
         경고한다.
    실패 조건: 다른 헬스체크와 달리 장중 여부로 게이팅하지 않는다 — 이 문제가 실제로 발견된
              시각도 07:30 장전이었다(옵션체인 REST 폴링은 장중 여부와 무관하게 구독이 롤링되는
              즉시 시작된다). 최근 구간에 콜/풋 데이터가 둘 다 없으면(폴링 미기동 등) 판단하지
              않고 정보로만 표시한다.
    """
    label = "옵션체인 콜/풋 균형(option_analysis_1m)"
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT option_type, count(*) FROM option_analysis_1m "
                "WHERE underlying=%s AND timestamp >= %s GROUP BY option_type",
                (underlying, now - timedelta(minutes=_OPTION_LEG_BALANCE_LOOKBACK_MINUTES)),
            )
            counts = dict(cur.fetchall())
    except Exception:
        conn.rollback()
        logger.warning("옵션체인 콜/풋 균형 점검 조회 실패", exc_info=True)
        return HealthCheck(label, "warning", "조회 실패")

    call_count = counts.get("C", 0)
    put_count = counts.get("P", 0)
    if not call_count and not put_count:
        return HealthCheck(label, "info", f"최근 {_OPTION_LEG_BALANCE_LOOKBACK_MINUTES}분간 데이터 없음")

    larger, smaller = max(call_count, put_count), min(call_count, put_count)
    if smaller / larger < _OPTION_LEG_BALANCE_MIN_RATIO:
        skewed_side = "풋" if put_count < call_count else "콜"
        return HealthCheck(
            label, "warning",
            f"콜 {call_count}건 / 풋 {put_count}건(최근 {_OPTION_LEG_BALANCE_LOOKBACK_MINUTES}분) — "
            f"{skewed_side} 조회만 계속 실패 중일 수 있음(레이트리밋 의심, NEXT_TODO 참고)",
        )
    return HealthCheck(
        label, "ok", f"콜 {call_count}건 / 풋 {put_count}건(최근 {_OPTION_LEG_BALANCE_LOOKBACK_MINUTES}분)"
    )


def _cbot_status_check(conn) -> HealthCheck:
    """
    해석: CME|CBOT 해외선물옵션 실시간시세는 KIS 유료 항목(2026-07-20 HTS [7936] 확인: 월
         228.8불)이라 모의투자 개발 단계에서는 미구독 상태다 — zn_front가 채워져 있어도 그
         출처(zn_front_source)가 "kis"가 아니라 "yfinance_fallback"이면 실제 CBOT 승인이
         아니라 mahdi/data/yfinance_fallback.py의 비공식 근사치이므로 "ok"로 표시하면 안 된다.
    """
    label = "CBOT(ZN/US10Y 선물) 데이터"
    try:
        snapshot = db.latest_macro_snapshot(conn)
    except Exception:
        conn.rollback()
        logger.warning("CBOT 상태 점검 조회 실패", exc_info=True)
        return HealthCheck(label, "warning", "조회 실패")
    if snapshot is None:
        return HealthCheck(label, "info", "아직 매크로 스냅샷 폴링 데이터 없음")
    zn_front = snapshot.get("zn_front")
    if zn_front is None:
        return HealthCheck(label, "info", "미승인 — zn_front NULL(KIS 앱/HTS 신청 상태 확인 필요)")
    if snapshot.get("zn_front_source") == "yfinance_fallback":
        return HealthCheck(label, "info", f"CBOT 미구독, yfinance 폴백 사용 중 — zn_front={zn_front:.2f}")
    return HealthCheck(label, "ok", f"승인됨 — zn_front={zn_front:.2f}")


def _macro_freshness_check(conn, now: datetime) -> HealthCheck:
    """
    계산: `macro_snapshot_5m` 최신 행의 나이를 신호 경로와 **같은 임계**
         (`regime_pipeline.MACRO_SNAPSHOT_MAX_AGE_MINUTES`)로 판정한다.
    해석: 2026-08-05(COCKPIT 육안 점검 P1-4) — 매크로는 **신선도를 보는 배지가 하나도 없던**
         유일한 데이터 경로였다. CBOT 배지가 같은 스냅샷을 읽지만 그건 `zn_front_source`가
         "kis"인지만 보므로, 폴러가 며칠 죽어 있어도 파란불("yfinance 폴백 사용 중")이 그대로 뜬다.
         임계를 신호 경로와 같은 값으로 두는 이유: 배지가 초록인데 `macro_score()`는 VIX 신호를
         버리고 있는(또는 그 반대) 상태를 만들지 않기 위함이다 — 배지와 판단이 다른 답을 내면
         어느 쪽을 믿을지 알 수 없다(`_signal_reach_check`와 같은 규약).
    실패 조건: 조회 실패는 "조회 실패"(warning). 폴링 이력이 아예 없으면 info.
    """
    label = "매크로 스냅샷 신선도"
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(timestamp) FROM macro_snapshot_5m")
            row = cur.fetchone()
        latest_ts = row[0] if row else None
    except Exception:
        conn.rollback()
        logger.warning("매크로 스냅샷 신선도 점검 조회 실패", exc_info=True)
        return HealthCheck(label, "warning", "조회 실패")
    if latest_ts is None:
        return HealthCheck(label, "info", "아직 매크로 스냅샷 폴링 이력 없음")
    age_minutes = max((now - _as_naive(latest_ts)).total_seconds(), 0.0) / 60.0
    detail = f"{_as_naive(latest_ts):%m-%d %H:%M} 기준 ({age_minutes:.0f}분 전)"
    if age_minutes > MACRO_SNAPSHOT_MAX_AGE_MINUTES:
        return HealthCheck(
            label, "warning",
            f"{detail} — 신호 경로가 VIX 기간구조를 버리는 상태(임계 {MACRO_SNAPSHOT_MAX_AGE_MINUTES}분)",
        )
    return HealthCheck(label, "ok", detail)


def _fossil_data_check(conn, underlying: str, now: datetime) -> HealthCheck:
    label = "화석 데이터(series/symbol 화이트리스트 위반)"
    try:
        fossil_series = db.expiry_liquidity_fossil_series(conn, underlying)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM market_raw_1m WHERE symbol=%s AND timestamp::date=%s",
                (_LEGACY_MIXED_SYMBOL, now.date()),
            )
            legacy_symbol_count = cur.fetchone()[0]
    except Exception:
        conn.rollback()
        logger.warning("화석 데이터 점검 조회 실패", exc_info=True)
        return HealthCheck(label, "warning", "조회 실패")
    problems = []
    if fossil_series:
        problems.append(f"expiry_liquidity_1m series={fossil_series}")
    if legacy_symbol_count:
        problems.append(f"market_raw_1m symbol='{_LEGACY_MIXED_SYMBOL}' {legacy_symbol_count}건(오늘)")
    if problems:
        return HealthCheck(label, "warning", "; ".join(problems))
    return HealthCheck(label, "ok", "화이트리스트 밖 데이터 없음")


def _schema_integrity_check(conn) -> HealthCheck:
    """
    해석: 2026-07-21 장전 점검에서 실측된 사고(마이그레이션 010/011이 파일로만 커밋되고
         라이브 컨테이너엔 반영 안 돼 macro_snapshot_5m 적재가 종일 실패 + COCKPIT이 그 여파로
         합성 폴백에 빠짐 — NEXT_TODO.md 참고) 재발을 대시보드에서 즉시 알아챌 수 있게 한다.
         db.macro_snapshot_columns()(코드가 실제로 쓰는 컬럼 목록, INSERT/SELECT와 단일 소스
         공유)를 information_schema.columns와 대조한다 — 두 쪽이 어긋나면 그 컬럼을 추가하는
         db/migrations/*.sql이 아직 라이브 DB에 적용 안 된 것.
    """
    label = "스키마 정합성(마이그레이션 적용 여부)"
    # 2026-08-05(P1-7): 대상 테이블을 늘렸다. 종전에는 macro_snapshot_5m 하나만 봤는데,
    # 마이그레이션 025로 regime_state에도 컬럼이 붙었다 — 미적용이면 레짐 적재가 실패하고
    # COCKPIT은 조회 실패로 **합성 폴백**에 빠진다(2026-07-21에 010/011로 실제로 겪은 그 사고).
    # 목록은 각 테이블의 단일 소스(db.*_columns())에서 가져온다.
    required = {
        "macro_snapshot_5m": db.macro_snapshot_columns(),
        "regime_state": db.regime_state_columns(),
        # 2026-08-05(P2-12, 마이그레이션 026): 미적용이면 WS 하트비트 기록이 실패하는데
        # `poll_ws_heartbeat`가 그 예외를 삼키므로(관측 자체는 계속돼야 하니 옳은 처리다)
        # WS 배지 3종이 **조용히** 멈춘다.
        "ws_status": db.ws_status_columns(),
    }
    missing_by_table: dict[str, list[str]] = {}
    try:
        for table, columns in required.items():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_name=%s",
                    (table,),
                )
                existing = {row[0] for row in cur.fetchall()}
            missing = [c for c in columns if c not in existing]
            if missing:
                missing_by_table[table] = missing
    except Exception:
        conn.rollback()
        logger.warning("스키마 정합성 점검 조회 실패", exc_info=True)
        return HealthCheck(label, "warning", "조회 실패")
    if missing_by_table:
        detail = "; ".join(f"{table}: {', '.join(cols)}" for table, cols in missing_by_table.items())
        return HealthCheck(label, "warning", f"없는 컬럼 — {detail} — db/migrations 라이브 적용 필요")
    return HealthCheck(label, "ok", f"{', '.join(required)} 컬럼 전부 정상")


def _regime_stability_check(conn, now: datetime) -> HealthCheck:
    label = "레짐 stability_flag 비율(오늘)"
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FILTER (WHERE stability_flag), count(*) FROM regime_state WHERE timestamp::date=%s",
                (now.date(),),
            )
            stable_count, total_count = cur.fetchone()
    except Exception:
        conn.rollback()
        logger.warning("레짐 안정성 점검 조회 실패", exc_info=True)
        return HealthCheck(label, "warning", "조회 실패")
    if not total_count:
        return HealthCheck(label, "info", "오늘 레짐 데이터 없음")
    pct = stable_count / total_count * 100
    # 낮은 비율 자체는 버그가 아니다(§3-3) — warmup_fallback()이 의도적으로 항상 False를
    # 반환하는 정상 동작일 수 있으므로 판단(ok/warning)이 아니라 정보로만 노출한다.
    return HealthCheck(label, "info", f"{pct:.0f}% 안정 ({stable_count}/{total_count}행) — 낮아도 버그 아님(§3-3)")


# 2026-07-19(§5-7 "20영업일 도달 카운트다운") — RegimeEngine.fit()을 실제로 게이팅하는 기준은
# scripts/fit_regime_engine.py의 DEFAULT_MIN_SAMPLES(행수)다. scripts/는 sys.path를 직접
# 조작하는 독립 실행 스크립트라 패키지처럼 안전하게 import하기 부적절해 값만 그대로 복제한다 —
# scripts/fit_regime_engine.py의 DEFAULT_MIN_SAMPLES를 바꾸면 이 값도 함께 맞출 것.
_REGIME_FIT_TARGET_ROWS = 8000
# v6 스펙/보고서가 쓰는 "20영업일"이라는 더 직관적인 단위 — 20세션 × 405분/세션 ≈ 8,100행이
# 근사 기준이라 위 행수 목표와 함께 보여준다.
_REGIME_FIT_TARGET_BUSINESS_DAYS = 20


def _regime_fit_progress_check(conn, underlying: str) -> HealthCheck:
    """
    계산: feature_store에 실제로 데이터가 쌓인 날짜 수(DISTINCT timestamp::date)와 총 행수를
         세어 scripts/fit_regime_engine.py 실행 시점까지 얼마나 남았는지 추정한다. 론치일부터
         달력으로 계산하지 않고 "실제로 데이터가 쌓인 날짜 수"를 직접 세는 이유: 스케줄러가
         쉬거나 실패한 날이 있어도(주말·공휴일 포함) 자동으로 정확하다 — 하드코딩된 론치일 +
         영업일 계산보다 항상 실제 축적 상태를 정확히 반영한다.
    """
    label = "레짐 엔진 학습 데이터(feature_store, 20영업일 목표)"
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*), count(DISTINCT timestamp::date) FROM feature_store "
                "WHERE symbol=%s AND feature_version=%s",
                (underlying, FEATURE_VERSION),
            )
            total_rows, distinct_days = cur.fetchone()
    except Exception:
        conn.rollback()
        logger.warning("레짐 학습 데이터 진행률 점검 조회 실패", exc_info=True)
        return HealthCheck(label, "warning", "조회 실패")

    if not total_rows:
        return HealthCheck(label, "info", "아직 feature_store 데이터 없음")

    if total_rows >= _REGIME_FIT_TARGET_ROWS:
        return HealthCheck(
            label, "ok",
            f"{total_rows:,}행 / {distinct_days}영업일 — 목표 도달, scripts/fit_regime_engine.py 실행 가능",
        )

    remaining_rows = _REGIME_FIT_TARGET_ROWS - total_rows
    avg_rows_per_day = total_rows / distinct_days if distinct_days else 0.0
    if avg_rows_per_day > 0:
        # 2026-08-06(운영점검 장전편 §2-6 / Fix#6) — `.0f`(반올림)가 아니라 `ceil`이다.
        # 부분 영업일에는 임계에 **도달하지 않는다** — 2.48영업일이 남았으면 도달일은 3영업일
        # 뒤다. 종전 표기는 08-06 화면에 "약 2영업일 남음"을 냈는데(7,032행 / 하루 391행),
        # 08-05 보고서 §9가 실제 도달일을 **08-10(월) = 3영업일 뒤**로 이미 확정해 둔 뒤였다.
        # 08-04 보고서가 "08-08(금)"이라는 존재하지 않는 날짜를 적었던 것과 같은 계열의 사고다 —
        # 일정은 낙관적으로 반올림하면 안 된다.
        eta_days = math.ceil(remaining_rows / avg_rows_per_day)
        eta_detail = f"약 {eta_days}영업일 남음(하루 평균 {avg_rows_per_day:.0f}행 기준 추정)"
    else:
        eta_detail = "누적 속도 계산 불가"
    return HealthCheck(
        label, "info",
        f"{total_rows:,}/{_REGIME_FIT_TARGET_ROWS:,}행 ({distinct_days}/{_REGIME_FIT_TARGET_BUSINESS_DAYS}영업일) — {eta_detail}",
    )


def _shutdown_reliability_check(conn) -> HealthCheck:
    """
    해석: 2026-07-21 §3-1에서 실측된 사고(15:45 자동 종료의 taskkill이 창 제목 매칭 실패로
         "No tasks running"만 남기고 실제로는 COCKPIT/관측 루프가 계속 살아있었는데 아무도
         알아채지 못함) 재발을 COCKPIT에서 바로 알아챌 수 있게 한다.
         scripts/log_marketclose_stop.py가 매 장마감 종료 시도마다 커맨드라인 기준으로 남은
         프로세스 수를 shutdown_check_log(싱글턴 테이블)에 기록한다 — 여기서는 그 최신 기록만
         읽는다(운영점검보고서 §5-3 "종료 신뢰성 배지").
    """
    label = "종료 신뢰성(직전 장마감)"
    try:
        result = db.latest_shutdown_check(conn)
    except Exception:
        conn.rollback()
        logger.warning("종료 신뢰성 점검 조회 실패", exc_info=True)
        return HealthCheck(label, "warning", "조회 실패")
    if result is None:
        return HealthCheck(label, "info", "기록 없음(마이그레이션 013 적용 전이거나 아직 장마감 종료 이력 없음)")
    checked_at, remaining = result
    if remaining <= 0:
        return HealthCheck(label, "ok", f"{checked_at:%Y-%m-%d %H:%M} 기준 정상 종료(잔존 프로세스 없음)")
    return HealthCheck(
        label, "warning",
        f"{checked_at:%Y-%m-%d %H:%M} 기준 프로세스 {remaining}개 잔존 — 수동 확인 필요",
    )


# 2026-07-23(운영점검보고서 §2-1/§4 Fix#4) — 배율이 이 값 이하면 "백오프 없음(정상)"으로 본다.
# 부동소수 계산 잔차를 허용하기 위해 정확히 1.0이 아니라 살짝 여유를 둔다.
_RATE_LIMITER_OK_MULTIPLIER = 1.01
# CB 감지 하트비트(mahdi.main.MARKET_HALT_HEARTBEAT_SECONDS=300초)의 2배 — 한 번 걸렀는데도
# 갱신이 없으면 관측 루프 쪽 이상으로 본다. 이 임계는 `updated_at`(독립 하트비트)에만 걸고,
# `last_message_at`(H0UNMKO0 수신)에는 걸지 않는다 — 정상일에도 수 시간 공백이 정상이기 때문이다
# (2026-07-31 실측: 하루 총 2건, 09:00 이후 6시간 45분 무수신이 정상 상태였다).
_MARKET_HALT_HEARTBEAT_STALE_SECONDS = 600.0

# ===== 2026-08-01(운영점검보고서 2026-07-31 §5-5) 관측 품질 배지 임계 =====
#
# 07-31에 "밀림 83→46건인데 먼슬리 커버리지 95.0%→90.5%"라는 사례가 있었다 — **인프라 지표는
# 전부 좋아졌는데 판단 입력 품질은 오히려 나빠졌다.** 인프라 배지만 보면 놓치므로 나란히 둔다.
_REST_DEMAND_WARNING_PCT = 60.0  # 07-31 실측 43.6%. 60%를 넘으면 폴러 추가를 멈추고 예산부터 본다.
# 백오프가 적자 임계에 이만큼 근접하면 경고 — 넘어가면 수요가 용량을 구조적으로 초과한다.
_BACKOFF_HEADROOM_WARNING_RATIO = 0.9
_MONTHLY_COVERAGE_WARNING_PCT = 95.0  # GEX/감마플립 입력의 1분 연속성(07-31 실측 90.3%)
_OVERRUN_COUNT_WARNING = 30  # 07-31 실측 46건. 페이서 분리 재개 조건과 같은 숫자다.
# 2026-08-05(P2-12) — 하루 ATM 롤 횟수 경고 임계. **잠정치다.**
#
# 20의 근거: 행사가 격자는 2.5p이므로 창이 움직이려면 선물이 격자 중간점(1.25p)을 넘어야 한다.
# 일중 실현 변동폭이 20~30p(1~3%)인 정상적인 날에 방향성 이동만으로 중간점을 넘는 횟수는
# 한 자릿수~십몇 회다. 20은 그 위에 여유를 준 값이다.
# 08-05 실측은 **77회**(08:46~12:20, 평균 2.7분마다) — 이 임계의 약 4배이고, 방향성 이동이 아니라
# 중간점 근처에서 오간 결과다(11:55 1035~1045 → 11:57 되돌림 → 12:01 재이동이 로그에 그대로 있다).
# 롤 로직에 히스테리시스가 없어서 생기는 현상인데, 그 수정은 구독 정책 변경이라 별건이다
# ([[NEXT_TODO]]) — 먼저 **보이게** 만든다. 며칠 실측이 쌓이면 이 값을 조정할 것.
_ATM_ROLL_WARNING = 20
# WS 하트비트(mahdi.main.WS_HEARTBEAT_SECONDS=300초)의 2배. CB 하트비트와 같은 기준이다.
_WS_HEARTBEAT_STALE_SECONDS = 600.0


def _rate_limiter_health_check(conn) -> HealthCheck:
    """
    해석: 07-22 저녁 레이트리미터 회복 임계값을 20->8로 낮췄다가 07-23 실측(EGW00201 비율·
         스케줄 밀림·평균/최대 지연 전부 악화, 운영점검보고서 §2-1)으로 다시 20으로 되돌렸다.
         그런데 그 판단조차 다음날 로그를 정밀분석해야만 가능했다 — 관측 루프(mahdi.main)와
         COCKPIT은 별도 프로세스라 레이트리미터의 실시간 배율을 COCKPIT이 직접 읽을 수 없다.
         mahdi.main의 poll_option_chain이 매 사이클(60초)마다 record_rate_limiter_status()로
         남긴 최신 배율·직전 사이클 밀림 초를 그대로 보여줘, 다음엔 사후 분석 없이 당일 바로
         악화 여부를 알 수 있게 한다.
    """
    label = "레이트리밋 근접도(공유 _RateLimiter)"
    try:
        result = db.latest_rate_limiter_status(conn)
    except Exception:
        conn.rollback()
        logger.warning("레이트리밋 근접도 점검 조회 실패", exc_info=True)
        return HealthCheck(label, "warning", "조회 실패")
    if result is None:
        return HealthCheck(label, "info", "기록 없음(마이그레이션 014 적용 전이거나 아직 폴링 이력 없음)")
    checked_at, multiplier, overrun_seconds = result
    detail = f"{checked_at:%H:%M:%S} 기준 배율 {multiplier:.2f}배, 직전 사이클 밀림 {overrun_seconds:.1f}초"
    if multiplier <= _RATE_LIMITER_OK_MULTIPLIER:
        return HealthCheck(label, "ok", detail)
    return HealthCheck(label, "warning", detail)


def _market_halt_check(conn) -> HealthCheck:
    """
    해석: 2026-07-29 신규 — 거래소 서킷브레이커/거래정지(mahdi.risk.market_halt) 실시간 감지
         결과를 "오늘의 점검 요약" 3초 룰 그리드에도 반영한다. 실제 발동 중일 때는 이 그리드
         배지뿐 아니라 app.py 상단에 별도의 눈에 띄는 배너(st.error)도 함께 뜬다 — 이 배지는
         "평소엔 조용히 정상, 스크롤 없이도 상시 확인 가능"용이고 배너는 "장중 놓칠 수 없게".
    """
    label = "거래소 서킷브레이커/거래정지"
    try:
        status = db.latest_market_halt_state(conn)
    except Exception:
        conn.rollback()
        logger.warning("서킷브레이커 상태 점검 조회 실패", exc_info=True)
        return HealthCheck(label, "warning", "조회 실패")
    if status is None:
        # 2026-07-30(운영점검 §2-4/§4 Fix#4): 관측 루프가 구독 직후 "정상" 행을 반드시 남기므로,
        # 여기서 None이라는 건 "CB가 없었다"가 아니라 **감지기가 아예 안 붙었다**는 뜻이다
        # (관측 루프 미기동/구독 실패). 예전엔 이 경우를 "정상"으로 표시해 라이브 검증 불가
        # 상태를 안심 신호로 덮고 있었다.
        return HealthCheck(label, "warning", "감지 상태 미기록 — 관측 루프 미기동 또는 구독 실패 여부 확인 필요")
    if status["is_halted"]:
        return HealthCheck(
            label, "warning",
            f"🚨 {status['label']}({status['mkop_cls_code']}) — {status['halted_since']:%H:%M:%S}부터 신규진입 차단 중",
        )
    # 2026-07-31(운영점검 §2-2/§4 우선순위 4): 이 배지는 서로 다른 세 가지를 구분해야 한다 —
    # (A) 감시 대상(시장)이 정상인가 (B) 감시자(관측 루프)가 살아있는가 (C) 감시자가 최근 실제로
    # 무언가를 봤는가. 07-30 설계는 A와 B를 `updated_at` 하나에 섞어 표시했는데, H0UNMKO0이
    # 세션 전이 시에만 오는 탓에 정상일에도 그 값이 6시간 45분 묵어 있었다.
    # 이제 `updated_at`은 독립 하트비트(300초)가 갱신하므로 **오래되면 진짜로 이상**이고,
    # `last_message_at`은 정상일에도 수 시간 공백이 정상이라 임계를 두지 않고 참고 표시만 한다.
    # 2026-07-31 라이브 왕복에서 잡은 결함: TIMESTAMPTZ 컬럼을 psycopg가 tz-aware로 돌려주는데
    # db.local_now()는 naive라 그대로 빼면 TypeError로 헬스체크 전체가 죽는다 — 2026-07-20에
    # `_freshness_check`에서 똑같이 겪었던 유형이다(그 docstring 참고). local_now()의 "naive-KST가
    # UTC 라벨로 저장된다"는 정책상 tzinfo만 떼면 벽시계 숫자는 이미 같은 좌표계다.
    heartbeat_age = (db.local_now() - _as_naive(status["updated_at"])).total_seconds()
    seen = status.get("last_message_at")
    seen_text = f" · 최근 장운영정보 {seen:%H:%M:%S}" if seen is not None else " · 장운영정보 수신 이력 없음"
    # 2026-08-03(운영점검 §2-4/§4 우선순위 4): (C)에 네 번째 질문을 더한다 — **감시자가 실제로
    # 감시 대상에 붙어 있는가**. 08-03에는 장운영정보 데이터가 하루 0건이었는데도 하트비트가
    # 정상이라 배지가 초록이었다. 데이터 수신에는 임계를 걸 수 없지만(정상일에도 0~2건) 구독
    # 성립 여부는 확정적으로 판정할 수 있다.
    ws_status_known, subscribed_at = _market_op_subscription(conn)
    subscribed_text = f" · 구독확립 {subscribed_at:%H:%M:%S}" if subscribed_at is not None else ""
    if heartbeat_age > _MARKET_HALT_HEARTBEAT_STALE_SECONDS:
        return HealthCheck(
            label, "warning",
            f"감지기 하트비트 {heartbeat_age / 60:.0f}분째 정지({status['updated_at']:%H:%M:%S}) — "
            f"관측 루프 생존 확인 필요{subscribed_text}{seen_text}",
        )
    if ws_status_known and subscribed_at is None:
        return HealthCheck(
            label, "warning",
            f"H0UNMKO0 구독 미성립 — 하트비트는 정상({status['updated_at']:%H:%M:%S})이지만 감지기가 "
            f"장운영정보에 붙어 있지 않다{seen_text}",
        )
    if status["mkop_cls_code"] is None:
        return HealthCheck(
            label, "ok",
            f"정상(발동 이력 없음) — 관측루프 {status['updated_at']:%H:%M:%S}{subscribed_text}{seen_text}",
        )
    return HealthCheck(
        label, "ok",
        f"정상 — 직전: {status['label']}({status['updated_at']:%H:%M:%S} 해제됨){subscribed_text}{seen_text}",
    )


def _market_op_subscription(conn) -> tuple[bool, datetime | None]:
    """
    계산: `ws_status`에서 (행이 있는가, H0UNMKO0 구독 성립 시각)을 읽는다.
    해석: 2026-08-03 §4 우선순위 4. 두 값을 함께 돌려주는 이유는 **"행이 없다"와 "행은 있는데
         구독만 안 걸렸다"가 전혀 다른 신호**이기 때문이다 — 전자는 마이그레이션 021 적용 전이거나
         관측 루프 미기동(그 판정은 `_ws_liveness_check`의 몫)이고, 후자만 이 배지가 경고할 일이다.
    실패 조건: 조회 실패는 (False, None) — CB 배지는 이 정보 없이도 나머지 판정을 계속해야 한다.
    """
    try:
        status = db.latest_ws_status(conn)
    except Exception:
        with contextlib.suppress(Exception):
            conn.rollback()
        logger.warning("WS 구독 성립 시각 조회 실패 — CB 배지의 나머지 판정은 계속한다", exc_info=True)
        return False, None
    if not status:
        return False, None
    return True, status.get("market_op_subscribed_at")


def get_market_halt_status() -> dict | None:
    """
    계산: app.py 상단 배너용 — `latest_market_halt_state()`를 그대로 반환한다(발동 중일 때만
         호출측이 st.error()로 크게 표시).
    실패 조건: DB 조회 실패 시 None — "정상"으로 거짓 안심시키지 않고 배너를 그냥 안 띄운다
              (오늘의 점검 요약 쪽 `_market_halt_check`가 "조회 실패"로 별도 표시하므로 이중
              경고가 되지 않도록 여기서는 조용히 로그만 남긴다).
    """
    try:
        with db.get_connection() as conn:
            return db.latest_market_halt_state(conn)
    except Exception:
        logger.warning("서킷브레이커 상태 조회 실패(배너용)", exc_info=True)
        return None


def _rest_demand_check(conn, now: datetime) -> HealthCheck:
    """
    해석: 2026-07-31에 처음 계량된 "총 REST 수요 43.6%"는 그때까지 **로그를 하루치 세야만** 알 수
         있는 값이었다 — COCKPIT은 배율만 볼 수 있었다. 마이그레이션 019로 누적 호출 수를 함께
         기록하게 되면서 당일 바로 볼 수 있다(§5-5). 계산은 일일 리포트와 **같은 함수**를 쓴다.
    """
    label = "REST 수요 / 페이서 용량"
    try:
        demand = db_metrics.rest_demand(conn, now.date())
    except Exception:
        conn.rollback()
        logger.warning("REST 수요 점검 조회 실패", exc_info=True)
        return HealthCheck(label, "warning", "조회 실패", group="관측 품질")
    pct = demand.get("capacity_pct")
    if pct is None:
        return HealthCheck(
            label, "info", "집계 전(마이그레이션 019 적용 전이거나 표본 2건 미만)", group="관측 품질"
        )
    detail = (
        f"{demand['calls_per_second']:.3f}건/초 = 용량의 {pct:.1f}% "
        f"(적자 시작 배율 {demand['deficit_threshold_multiplier']:.2f}배)"
    )
    status = "warning" if pct >= _REST_DEMAND_WARNING_PCT else "ok"
    return HealthCheck(label, status, detail, group="관측 품질")


def _backoff_headroom_check(conn, now: datetime) -> HealthCheck:
    """오늘 최대 백오프 배율이 적자 임계에 얼마나 근접했는지 — 넘으면 수요가 용량을 초과한다."""
    label = "백오프 여유(적자 임계 대비)"
    try:
        demand = db_metrics.rest_demand(conn, now.date())
        limiter = db_metrics._rate_limiter(conn, now.date())
    except Exception:
        conn.rollback()
        logger.warning("백오프 여유 점검 조회 실패", exc_info=True)
        return HealthCheck(label, "warning", "조회 실패", group="관측 품질")
    threshold = demand.get("deficit_threshold_multiplier")
    observed = limiter.get("max_multiplier")
    if threshold is None or observed is None:
        return HealthCheck(label, "info", "집계 전", group="관측 품질")
    ratio = observed / threshold
    detail = f"오늘 최대 {observed:.2f}배 / 적자 임계 {threshold:.2f}배 ({ratio * 100:.0f}%)"
    status = "warning" if ratio >= _BACKOFF_HEADROOM_WARNING_RATIO else "ok"
    return HealthCheck(label, status, detail, group="관측 품질")


def _monthly_coverage_check(conn, underlying: str, now: datetime) -> HealthCheck:
    """
    해석: **GEX/감마플립 입력의 1분 연속성.** 07-31에 인프라 지표(밀림 83→46건)가 좋아지는 동안
         이 값은 95.0% → 90.5%로 후퇴했다 — 인프라 배지 옆에 두지 않으면 놓치는 종류의 지표다.
    """
    label = "먼슬리 분 커버리지"
    try:
        # 2026-08-03 COCKPIT 육안 점검: 종전에는 분모로 "09:00 이후 경과 분"을 직접 계산해
        # 넘겼는데, 분자(monthly_book_coverage)는 하루 전체(장전 07:32~)를 센다 — 기간이 어긋나
        # **120.7%**가 나왔고 배지는 `< 95%`만 경고하므로 초록불이었다. 인자를 넘기지 않으면
        # 분자와 같은 구간(observed_span_minutes)을 분모로 쓴다. 장전/장후에도 유효해진다.
        coverage = db_metrics.monthly_book_coverage(conn, now.date(), underlying=underlying)
    except Exception:
        conn.rollback()
        logger.warning("먼슬리 커버리지 점검 조회 실패", exc_info=True)
        return HealthCheck(label, "warning", "조회 실패", group="관측 품질")
    pct = coverage.get("coverage_pct")
    if pct is None:
        # 만기유동성 폴러의 첫 행이 08:31 부근이라 그 전에는 먼슬리 만기를 특정할 수 없다.
        return HealthCheck(label, "info", coverage.get("reason") or "집계 전", group="관측 품질")
    detail = (
        f"{coverage['minutes']}분 / 관측 {coverage['elapsed_minutes']}분 = {pct:.1f}% "
        f"(만기 {coverage['expiry']})"
    )
    if coverage.get("over_100"):
        # 커버리지가 100%를 넘는 건 데이터가 좋다는 뜻이 아니라 **지표가 고장났다**는 뜻이다.
        return HealthCheck(label, "warning", f"{detail} — 100% 초과: 분자/분모 기간 불일치", group="관측 품질")
    status = "warning" if pct < _MONTHLY_COVERAGE_WARNING_PCT else "ok"
    return HealthCheck(label, status, detail, group="관측 품질")


def _signal_reach_check(conn, now: datetime) -> HealthCheck:
    """
    해석: 2026-08-03 §5-1 — **커버리지(§12)가 답하지 못하는 한 칸.** 커버리지는 "데이터가 DB에
         있는가"만 재고, 이 배지는 "그 데이터가 판단까지 도달했는가"를 잰다. 08-03에 먼슬리
         커버리지 98.8%인 날 감마플립 산출률은 0%였고, 그 사실을 볼 수 있는 지표가 없었다.
         **리포트(`mahdi/ops/report.py` §14)와 같은 함수·같은 임계를 쓴다** — 배지와 리포트가
         다른 답을 내면 어느 쪽을 믿을지 알 수 없다(README 규약).
    실패 조건: 마이그레이션 022 적용 전이거나 아직 판단 이력이 없으면 "집계 전"(info).
    """
    label = "신호 도달률(체인 → 판단)"
    try:
        reach = db_metrics.signal_reach(conn, now.date())
    except Exception:
        conn.rollback()
        logger.warning("신호 도달률 점검 조회 실패", exc_info=True)
        return HealthCheck(label, "warning", "조회 실패", group="관측 품질")
    if not reach.get("available"):
        return HealthCheck(
            label, "info", "집계 전(마이그레이션 022 적용 전이거나 판단 이력 없음)", group="관측 품질"
        )
    detail = (
        f"감마플립 {reach['gamma_flip_pct']}% ({reach['gamma_flip_count']:,}/{reach['decisions']:,}분) · "
        f"앙상블 최대 {reach['member_count_max']}멤버"
    )
    age_max = reach.get("chain_age_seconds_max")
    if age_max is not None:
        detail += f" · 체인 최고령 {age_max / 60:.0f}분"
    warnings = reach.get("warnings") or []
    if warnings:
        return HealthCheck(label, "warning", f"{detail} — {warnings[0]}", group="관측 품질")
    # 2026-08-06 Fix#5 — 장전에는 판정을 유예한다. **숨기지는 않는다**: 같은 문장을 info로 낸다.
    # 노란불이 아니어야 하는 이유는 §2-5에 있다(설계대로 동작한 것을 매일 90분씩 장애로 신고했다).
    notes = reach.get("notes") or []
    if notes:
        return HealthCheck(label, "info", f"{detail} — {notes[0]}", group="관측 품질")
    return HealthCheck(label, "ok", detail, group="관측 품질")


def _entry_cutoff_check(conn, now: datetime) -> HealthCheck:
    """
    해석: 2026-08-06 §2-2 / Fix#1 — **진입이 없는 이유를 화면이 설명해야 한다.**
         14:50을 넘기면 v6 §4.2에 따라 신규 진입이 금지되는데, 배지가 없으면 그 시각 이후의
         "진입 0건"이 신호가 죽은 것인지 규칙이 걸린 것인지 화면에서 구분되지 않는다.
         `enter_after_cutoff`는 **0이어야 하는 불변식**이다 — 08-06에는 21건이었다(게이트가
         코드에 없었다). 0이 아니면 빨간 신호가 아니라 노란불로 낸다: 이 배지가 잡는 것은
         시장 이상이 아니라 **우리 코드의 회귀**다.
    실패 조건: 판단 이력이 없으면 "집계 전"(info) — 지어내지 않는다.
    """
    label = "진입 컷오프(14:50)"
    try:
        stats = db_metrics.decisions(conn, now.date())
    except Exception:
        conn.rollback()
        logger.warning("진입 컷오프 점검 조회 실패", exc_info=True)
        return HealthCheck(label, "warning", "조회 실패", group="판단")
    if not stats.get("total"):
        return HealthCheck(label, "info", "집계 전(오늘 판단 이력 없음)", group="판단")
    cutoff = stats["entry_cutoff"]
    violated = cutoff["enter_after_cutoff"]
    if violated:
        return HealthCheck(
            label, "warning",
            f"컷오프 이후 ENTER {violated}건 — 게이트 회귀"
            f"(그중 강제 평탄화 이후 {cutoff['enter_after_forced_flat']}건)",
            group="판단",
        )
    if session.is_after_entry_cutoff(now):
        return HealthCheck(
            label, "info",
            f"컷오프 경과 — 신규 진입 금지 중(오늘 {cutoff['blocked_count']}분 차단)",
            group="판단",
        )
    return HealthCheck(
        label, "ok", f"{cutoff['cutoff_time']}까지 진입 가능 · 오늘 ENTER {stats['decision'].get('ENTER', 0)}건",
        group="판단",
    )


def _overrun_count_check(conn, now: datetime) -> HealthCheck:
    """당일 누적 스케줄 밀림 — 다음날 로그를 뒤지지 않고 그날 바로 악화를 본다."""
    label = "스케줄 밀림(당일 누적)"
    try:
        limiter = db_metrics._rate_limiter(conn, now.date())
    except Exception:
        conn.rollback()
        logger.warning("밀림 누적 점검 조회 실패", exc_info=True)
        return HealthCheck(label, "warning", "조회 실패", group="관측 품질")
    if not limiter.get("rows"):
        return HealthCheck(label, "info", "아직 사이클 이력 없음", group="관측 품질")
    count = limiter["overrun_rows"]
    detail = f"{count}건 / 사이클 {limiter['rows']}건 (평균 배율 {limiter['mean_multiplier']:.2f}배)"
    status = "warning" if count >= _OVERRUN_COUNT_WARNING else "ok"
    return HealthCheck(label, status, detail, group="관측 품질")


def _ws_liveness_check(conn, now: datetime) -> HealthCheck:
    """
    해석: 2026-08-01 §5-4 — 07-31 WS 재연결 **0회**였지만 재연결 로직이 살아있는지는 증명되지
         않았다(CB 감지와 같은 구조의 사각지대). 이제 독립 하트비트가 `updated_at`을 갱신하므로
         **오래됐다는 건 관측 루프의 WS 파트가 멈췄다는 뜻**이다.
         `last_message_at`에는 **장중에만** 임계를 건다 — 장외에는 체결이 없어 비어 있는 게
         정상이고, 여기에 임계를 걸면 CB 감지에서 겪은 상시 오경보를 반복한다.
    """
    label = "WS 연결/재연결 감지"
    try:
        status = db.latest_ws_status(conn)
    except Exception:
        conn.rollback()
        logger.warning("WS 생존 점검 조회 실패", exc_info=True)
        return HealthCheck(label, "warning", "조회 실패", group="관측 품질")
    if status is None:
        return HealthCheck(
            label, "warning", "감지 상태 미기록 — 관측 루프 미기동 여부 확인 필요", group="관측 품질"
        )
    age = (now - _as_naive(status["updated_at"])).total_seconds()
    reconnects = status["reconnect_count_today"]
    seen = status["last_message_at"]
    seen_text = f"최근 수신 {_as_naive(seen):%H:%M:%S}" if seen else "수신 이력 없음"
    if age > _WS_HEARTBEAT_STALE_SECONDS:
        return HealthCheck(
            label, "warning",
            f"하트비트 {age / 60:.0f}분째 정지({_as_naive(status['updated_at']):%H:%M:%S}) — "
            f"관측 루프 WS 파트 확인 필요 · {seen_text}",
            group="관측 품질",
        )
    since = status["connected_since"]
    since_text = f"{_as_naive(since):%H:%M:%S}부터" if since else "연결 시각 미기록"
    detail = f"연결 {since_text} · 오늘 재연결 {reconnects}회 · {seen_text}"
    # 재연결이 일어났다는 것 자체는 이상 신호다(0회가 정상) — 다만 지금 붙어 있으면 경고까지는 아니다.
    status_level = "warning" if reconnects > 0 else "ok"
    return HealthCheck(label, status_level, detail, group="관측 품질")


def _atm_roll_churn_check(conn, now: datetime) -> HealthCheck:
    """
    계산: 오늘 ATM 행사가 창이 실제로 이동한 횟수(`ws_status.atm_roll_count_today`,
         마이그레이션 026)를 `_ATM_ROLL_WARNING`과 대조한다.
    해석: 2026-08-05(COCKPIT 육안 점검 P2-12) — **관측 연속성의 선행지표.** 롤 1회마다 창을
         벗어난 종목의 WS 구독이 해제돼 그 종목 1분봉이 끊긴다. 08-05에 Flow Radar 옵션이
         11:26~12:02를 직선으로 그린 것(P0-1)이 그 결과였고, 그 35분은 구독 해제 구간이었다.
         P0-1로 화면은 이제 그 공백을 정직하게 그리지만, **공백 자체를 줄이려면 롤 횟수를 봐야
         한다** — 그런데 그 값은 로그의 "ATM 롤링" 줄을 세야만(그것도 북 수로 나눠야만) 알 수
         있었다. 07-31에 REST 수요를 계량하기 전과 같은 상태다.
    실패 조건: 조회 실패는 warning. `ws_status` 행이 없으면(관측 루프 미기동/마이그레이션 020
              미적용) info — 그 판정은 `_ws_liveness_check`의 몫이라 여기서 중복 경고하지 않는다.
    """
    label = "ATM 행사가 창 롤 횟수(오늘)"
    try:
        status = db.latest_ws_status(conn)
    except Exception:
        conn.rollback()
        logger.warning("ATM 롤 횟수 점검 조회 실패", exc_info=True)
        return HealthCheck(label, "warning", "조회 실패", group="관측 품질")
    if status is None:
        return HealthCheck(label, "info", "감지 상태 미기록", group="관측 품질")
    count = status["atm_roll_count_today"]
    if count is None:
        # 아직 아무도 안 셌다. 원인은 둘 중 하나이고 **화면에서는 구분되지 않는다**:
        #   (a) 마이그레이션 026 미적용 — 스키마 정합성 배지가 그 사실을 따로 경고한다.
        #   (b) 컬럼은 있는데 관측 루프가 이 값을 모르는 구 코드로 떠 있다(배포 당일에 실제로 겪음).
        # 어느 쪽이든 **0으로 지어내면 "롤이 없었다"는 거짓말**이 되므로 "집계 전"만 말한다.
        return HealthCheck(
            label, "info",
            "집계 전 — 이 값을 세는 관측 루프가 아직 안 떴습니다(마이그레이션 026 미적용이거나 구 코드 실행 중)",
            group="관측 품질",
        )
    detail = f"{count}회 — 롤마다 창을 벗어난 종목의 1분봉이 끊긴다(Flow Radar 공백의 원인)"
    if count >= _ATM_ROLL_WARNING:
        return HealthCheck(
            label, "warning",
            f"{detail} · 임계 {_ATM_ROLL_WARNING}회 초과 — 히스테리시스 없이 격자 중간점을 오가는지 확인",
            group="관측 품질",
        )
    return HealthCheck(label, "ok", detail, group="관측 품질")


def get_health_summary(underlying: str = "KOSPI200") -> list[HealthCheck]:
    """
    입력: 기초자산 라벨.
    계산: 운영점검보고서 §1-B 장중 체크리스트 중 SQL로 자동화 가능한 항목들(§5-6 "오늘의 점검
         요약") — 거래소 서킷브레이커/거래정지(2026-07-29 추가), 옵션체인/선물 데이터 결손,
         옵션체인 콜/풋 균형(2026-07-20 추가), CBOT 승인
         상태, 스키마 정합성/마이그레이션 적용 여부(2026-07-21 추가), series/symbol 화석 데이터
         잔존 여부, 오늘 레짐 stability_flag 비율, feature_store 20영업일 목표 진행률(§5-7),
         직전 장마감 종료 신뢰성(2026-07-21 §5-3 추가), 레이트리밋 근접도(2026-07-23 §2-1/§4
         Fix#4 추가) — 을 매번 사람이 DB를 직접 조회하지 않고 COCKPIT 상단에서 바로 볼 수 있게
         한다.
    실패 조건: 항목별로 독립적으로 조회한다 — 하나가 실패해도(쿼리 오류 등) rollback 후 나머지
              항목은 계속 보여준다. DB 연결 자체가 안 되면 단일 "조회 불가" 항목 하나만 반환한다.
    """
    try:
        with db.get_connection() as conn:
            now = db.local_now()
            return [
                _market_halt_check(conn),
                _option_chain_freshness_check(conn, underlying, now),
                _futures_freshness_check(conn, underlying, now),
                _option_chain_leg_balance_check(conn, underlying, now),
                _cbot_status_check(conn),
                # 2026-08-05(P1-4) — CBOT 배지 바로 옆. 그 배지는 같은 스냅샷을 읽지만 출처만
                # 보므로, 폴러가 죽어도 "yfinance 폴백 사용 중"으로 파란불이 그대로 뜬다.
                _macro_freshness_check(conn, now),
                _schema_integrity_check(conn),
                _fossil_data_check(conn, underlying, now),
                _regime_stability_check(conn, now),
                _regime_fit_progress_check(conn, underlying),
                _shutdown_reliability_check(conn),
                _rate_limiter_health_check(conn),
                # 2026-08-01(§5-5) 관측 품질 — 인프라 지표가 좋아져도 판단 입력 품질은 나빠질 수
                # 있다(07-31 실측). 두 그룹을 나란히 봐야 그 어긋남이 보인다.
                _rest_demand_check(conn, now),
                _backoff_headroom_check(conn, now),
                _monthly_coverage_check(conn, underlying, now),
                _overrun_count_check(conn, now),
                _ws_liveness_check(conn, now),
                # 2026-08-05(P2-12) — WS 생존 배지 바로 옆. 같은 구독 도메인이고, P0-1에서 정직하게
                # 그리기 시작한 Flow Radar 공백의 **원인 쪽** 지표다.
                _atm_roll_churn_check(conn, now),
                # 2026-08-03(§5-1) — 커버리지 바로 아래 칸. 08-03에 커버리지 98.8%인 날
                # 감마플립 산출률은 0%였다(데이터는 DB에 있었지만 판단까지 가지 않았다).
                _signal_reach_check(conn, now),
                # 2026-08-06(§2-2 / Fix#1) — 진입이 없는 이유를 화면이 설명하게 한다.
                _entry_cutoff_check(conn, now),
            ]
    except Exception:
        logger.warning("점검 요약 조회 실패", exc_info=True)
        return [HealthCheck("오늘의 점검 요약", "warning", "DB 연결 실패로 조회 불가")]


def get_latest_decision_context(limit: int = 20) -> dict:
    """
    입력: 이력으로 함께 보여줄 최대 건수.
    계산: 2026-07-29 신규 "마흐디 판단 현황" 패널용 — `signal_decisions`의 최신 1건과 최근
         `limit`건 이력을 함께 반환한다(ADVISORY 전용, 실주문 없음 — Signal Fusion이 지금 어떤
         진입 판단을 내리고 있는지만 보여준다).
    해석: `latest["risk_gate_state"]["risk_engine"]`은 dict(승인/거부) 또는 문자열
         `"account_tracker_not_ready"`(계좌 잔고 폴러가 아직 안 돌았음)일 수 있다 — 호출측
         (decision_panel)이 두 경우를 구분해서 보여줘야 한다.
    실패 조건: DB 조회 실패 시 `{"latest": None, "history": []}`로 폴백(지어내지 않음).
    """
    try:
        with db.get_connection() as conn:
            history = db.recent_signal_decisions(conn, limit=limit)
    except Exception:
        logger.warning("판단 현황 조회 실패", exc_info=True)
        return {"latest": None, "history": []}
    return {"latest": history[0] if history else None, "history": history}


def get_account_status_view() -> dict | None:
    """
    계산: 2026-07-29 신규 "계좌 현황" 패널용. 최신 계좌 잔고 스냅샷 + 오늘/이번주 자정 이전
         baseline + 역대 최고치로 `mahdi.execution.account_tracker.build_account_state()`를
         그대로 호출해 일간/주간 손익률·드로우다운을 계산한다(`RiskEngine`이 쓰는 것과 동일한
         함수 재사용 — 손익률 계산을 두 곳에서 따로 하지 않는다).
    해석: `build_account_state()`는 원래 "이 방향으로 진입했을 때"를 가정하는 함수라
         `candidate_side`를 요구하는데, 이 화면은 방향과 무관한 계좌 현황 표시라 "BUY"를 고정
         전달하고 반환값의 `same_direction_positions`(candidate_side에 종속)는 쓰지 않는다 —
         `daily_pnl_pct`/`weekly_pnl_pct`/`drawdown_pct`는 candidate_side와 무관하다.
    실패 조건: 스냅샷이 아직 하나도 없으면(계좌 잔고 폴러 미기동) `None` — 손익 0으로 지어내지
              않고 "데이터 없음"을 그대로 호출측(account_panel)에 알린다.
    """
    try:
        with db.get_connection() as conn:
            latest_row = db.latest_account_balance_snapshot(conn)
            if latest_row is None:
                return None
            now = db.local_now()
            today_midnight = datetime.combine(now.date(), dtime.min)
            week_start_midnight = today_midnight - timedelta(days=today_midnight.weekday())
            latest = BalanceSnapshot(**latest_row)
            day_before_row = db.account_balance_snapshot_before(conn, today_midnight)
            week_before_row = db.account_balance_snapshot_before(conn, week_start_midnight)
            peak = db.max_account_balance_ever(conn)
            account_state = build_account_state(
                latest,
                BalanceSnapshot(**day_before_row) if day_before_row is not None else None,
                BalanceSnapshot(**week_before_row) if week_before_row is not None else None,
                peak,
                candidate_side="BUY",
                daily_trades_by_strategy={},
            )
    except Exception:
        logger.warning("계좌 현황 조회 실패", exc_info=True)
        return None
    return {
        "timestamp": latest.timestamp,
        "prsm_dpast": latest.prsm_dpast,
        "dnca_cash": latest.dnca_cash,
        "ord_psbl_cash": latest.ord_psbl_cash,
        "mgna_tota": latest.mgna_tota,
        "evlu_pfls_amt_smtl": latest.evlu_pfls_amt_smtl,
        "trad_pfls_amt_smtl": latest.trad_pfls_amt_smtl,
        "daily_pnl_pct": account_state.daily_pnl_pct,
        "weekly_pnl_pct": account_state.weekly_pnl_pct,
        "drawdown_pct": account_state.drawdown_pct,
        # 2026-08-05(COCKPIT 육안 점검 P1-5) — **비교 기준이 있었는가.**
        #
        # `build_account_state()`는 baseline/peak이 없으면 0.0으로 흡수한다(그 docstring: "손익
        # 없음이 아니라 아직 비교할 과거가 없다는 뜻이지만, 리스크 게이트 관점에서 구분할 필요는
        # 없다"). **RiskEngine에는 맞는 말이지만 화면에는 틀리다** — 08-05 화면의 일간/주간
        # +0.00%와 최대낙폭 +0.00%는 "변동 없음"이 아니라 "기준이 없음"이었는데 둘이 구분되지
        # 않았다. 이 프로젝트가 `atm_straddle_vrp()`에 명문화한 규약("입력이 규약을 만족하지
        # 않으면 값을 지어내지 말고 None을 낸다")과 어긋난다.
        #
        # 여기서 pct 자체를 None으로 바꾸지 않는 이유: 그 값은 RiskEngine과 **같은 함수**가 낸
        # 것이라 화면이 임의로 손대면 두 곳이 갈린다. 대신 "기준이 있었는가"를 함께 실어
        # 표시 계층(account_panel)이 판단하게 한다.
        "has_daily_baseline": day_before_row is not None,
        "has_weekly_baseline": week_before_row is not None,
        "has_peak": peak is not None,
    }


def load_snapshot(underlying: str = "KOSPI200") -> DashboardSnapshot:
    live = _load_from_db(underlying)
    return live if live is not None else _synthetic_snapshot()


def _gamma_wall_strikes(legs: list, spot: float) -> list[float]:
    """
    입력: **한 북**의 옵션 레그(`signal_book_legs()` 결과), 기초자산 스팟.
    계산: 감마 월 후보 행사가를 관측 루프(`main._build_signal_inputs`)와 **완전히 같은 규칙**으로
         구한다 — `gamma_walls(top_n=1)` + **노출 > 0 가드**.
    해석: 2026-08-05(COCKPIT 육안 점검 P0-3). 두 가지가 어긋나 있었다.

         ① `gamma_walls()`는 행사가별 |gamma x OI x ...| 합을 내림차순으로 줄 뿐이라, **OI가 전부
            0이어도 1등은 나온다.** 관측 루프는 이걸 알고 `walls[0][1] > 0`로 막고 있었는데
            (main.py, 2026-08-04) COCKPIT은 노출값을 버리고 행사가만 취해 가드가 없었다 —
            `find_gamma_flip()`이 전 구간 0인 곡선에서 허수 flip을 냈던 것과 같은 종류의 결함이다.
         ② 엔진은 top_n=1인데 COCKPIT은 기본값 3이었다. 행사가 창이 ATM±2(5개)뿐이라 "5개 중
            3개가 월"이 되어 순위의 정보량이 사실상 없었고(08-05 화면 GW1/GW2/GW3가 1050/1040/
            1047.5 = 창의 양 끝), 무엇보다 **화면의 월과 판단이 쓰는 월이 달랐다.**
    실패 조건: 레그가 없거나 최상위 노출이 0 이하면 빈 목록 — 선을 긋지 않는다(값을 지어내지 않음).
    """
    if not legs:
        return []
    walls = compute_gamma_walls(legs, spot, top_n=1)
    if not walls or walls[0][1] <= 0:
        return []
    return [walls[0][0]]


def _load_from_db(underlying: str) -> DashboardSnapshot | None:
    try:
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                # is_warmup(마이그레이션 025, 2026-08-05 P1-7): prob_vector가 학습된 확률인지
                # warmup_fallback()의 one-hot 상수인지 — 화면이 그 둘을 같게 그리면 안 된다.
                # 마이그레이션 미적용 구간(커밋 ~ 다음 장전 기동)에도 **표시용 컬럼 하나 때문에
                # 화면 전체가 합성 데이터로 떨어지면 안 되므로** 없어도 계속한다(P2-10 검증 중 실측 —
                # `db._select_with_optional_columns()` docstring 참고).
                regime_row = db._select_with_optional_columns(
                    conn,
                    base="SELECT timestamp, regime, prob_vector, higher_tf_regime, stability_flag",
                    optional=("is_warmup",),
                    tail=" FROM regime_state ORDER BY timestamp DESC LIMIT 1",
                )
                if regime_row is None:
                    return None
                # 리런 시각(datetime.now())이 아니라 스냅샷 자체의 최신 시각을 룩백 기준으로 쓴다 —
                # 장 마감 후 리플레이/재현 시나리오에서도 "최근 N분"이 항상 실제 데이터 시각 기준으로
                # 맞아야 하기 때문(datetime.now() 기준이면 지난 데이터를 볼 때 항상 윈도가 텅 빈다).
                as_of_ts = regime_row[0]

            # 2026-08-05(P1-6): 값과 **그 관측 시각**을 함께 읽는다 — 화면이 "언제 것인지"를
            # 표시할 수 있어야 한다(`latest_underlying_spot_row()` docstring 참고).
            spot_row = db.latest_underlying_spot_row(conn, underlying)
            if spot_row is None:
                return None
            spot, spot_asof = spot_row

            chain_rows = db.latest_option_chain(conn, underlying)
            investor_flow = db.latest_investor_flow(conn, underlying)
            expiry_liquidity = db.latest_expiry_liquidity(conn, underlying)
            # 매크로 폴러(poll_macro_snapshot)는 다른 폴러들과 별개 실패 도메인(해외선물옵션
            # 계좌 제약 등)이라, 이 조회 하나가 실패해도 대시보드 전체가 합성 폴백으로 떨어지면
            # 안 된다 — 독립적으로 감싸 None으로만 처리한다.
            try:
                macro_snapshot = db.latest_macro_snapshot(conn)
            except Exception:
                conn.rollback()
                logger.warning("매크로 스냅샷 조회 실패", exc_info=True)
                macro_snapshot = None

            # 선물 계열: active_futures_symbol 레지스트리로 현재 구독 중인 선물 단축코드를
            # 명시적으로 조회한다(vpin 유무 같은 휴리스틱에 더 이상 의존하지 않음 — 2026-07-06,
            # 옵션에도 VPIN을 적용하면서 그 휴리스틱이 깨졌기 때문).
            futures_flow_symbol = db.get_active_futures_symbol(conn, underlying)

            # 2026-08-05(P2-9) — 두 Flow Radar 계열이 **같은 시간 창**을 보게 한다. 종전에는
            # 행 수 상한(LIMIT 60)이라 선물은 우연히 60분이었지만 거래가 뜸한 옵션은 몇 시간에
            # 걸쳤고, x축만 선물 창으로 강제돼 창 밖 점이 y축만 잡아늘였다(상세 근거는
            # `FLOW_RADAR_WINDOW_MINUTES` 주석).
            flow_cutoff = as_of_ts - timedelta(minutes=FLOW_RADAR_WINDOW_MINUTES)

            with conn.cursor() as cur:
                futures_rows: list = []
                if futures_flow_symbol is not None:
                    cur.execute(
                        "SELECT timestamp, close, ofi, microprice, vpin FROM market_raw_1m "
                        "WHERE symbol=%s AND timestamp >= %s ORDER BY timestamp DESC LIMIT %s",
                        (futures_flow_symbol, flow_cutoff, FLOW_RADAR_ROW_CAP),
                    )
                    futures_rows = cur.fetchall()

                # 옵션 계열: 선물이 WS 구독 덕에 거의 매분 체결돼 "가장 최근 활동"만으로 뽑으면
                # 옵션이 영원히 안 뽑힌다 — 선물 심볼과 화석 라벨을 명시적으로 제외한다. 단일 최근
                # 틱이 아니라 최근 룩백 윈도 누적거래량 기준으로 뽑아야 동률 타이밍에 매 리런마다
                # 종목이 바뀌는 문제(2026-07-06 위클리 도입 후 실측)가 없다. symbol ASC는 남은
                # 동률(거래량·시각 모두 같음)까지 결정론적으로 고정하기 위한 최종 타이브레이커.
                excluded_symbols = (_LEGACY_MIXED_SYMBOL, futures_flow_symbol or _LEGACY_MIXED_SYMBOL)
                lookback_cutoff = as_of_ts - timedelta(minutes=FLOW_RADAR_OPTION_LOOKBACK_MINUTES)
                cur.execute(
                    "SELECT symbol FROM market_raw_1m WHERE symbol NOT IN (%s, %s) AND timestamp >= %s "
                    "GROUP BY symbol ORDER BY sum(volume) DESC, max(timestamp) DESC, symbol ASC LIMIT 1",
                    (*excluded_symbols, lookback_cutoff),
                )
                option_row = cur.fetchone()
                option_flow_symbol = option_row[0] if option_row else None

                option_rows: list = []
                if option_flow_symbol is not None:
                    cur.execute(
                        "SELECT timestamp, close, ofi, microprice, vpin FROM market_raw_1m "
                        "WHERE symbol=%s AND timestamp >= %s ORDER BY timestamp DESC LIMIT %s",
                        (option_flow_symbol, flow_cutoff, FLOW_RADAR_ROW_CAP),
                    )
                    option_rows = cur.fetchall()
    except Exception:
        # DB 미가동·마이그레이션 전·연결 실패 등 — 대시보드는 합성 데이터로 계속 동작해야 한다.
        # 2026-07-06: 예전엔 여기서 조용히 None만 반환해 왜 합성 폴백으로 빠졌는지 사후에 알 수
        # 없었다(오래 떠 있던 COCKPIT 프로세스가 옛 코드를 캐싱한 채 계속 폴백하던 사고) — 원인
        # 추적이 가능하도록 로그를 남긴다.
        logger.exception("실시간 데이터 조회 실패 — 합성 리플레이로 폴백")
        return None

    futures_rows = list(reversed(futures_rows))
    option_rows = list(reversed(option_rows))
    ts, regime_idx, prob_vector, higher_tf_idx, stability_flag, is_warmup = regime_row
    regime_prob = {RegimeLabel(i): float(p) for i, p in enumerate(prob_vector)}

    today = db.local_now().date()
    # 2026-08-05(COCKPIT 육안 점검 P0-2) — **화면과 판단이 같은 체인을 보게 한다.**
    #
    # 종전에는 여기서 `legs`를 인라인으로 만들며 `chain_rows` 전체(먼슬리 + 위클리 월·목 세 북)를
    # 평탄화했고, GEX 막대도 만기 구분 없이 행사가별로 합산했다. 그런데 관측 루프는 2026-08-04
    # Fix#5로 **먼슬리 한 북만** 쓰도록 이미 고쳐져 있었다(`signal_book_legs()` docstring의 실측
    # 피해 참고) — 즉 **COCKPIT만 Fix#5 이전 상태로 남아 있었다.** 08-05 화면 실측: 잔존 1일
    # 위클리(t=1/365)의 감마가 압도적이라 GEX 프로파일을 사실상 그 북이 지배하는데, 화면에는
    # 그 사실을 알릴 표시가 없었다.
    #
    # 이제 엔진과 **같은 함수**를 호출한다(배지와 리포트에 같은 함수를 강제하는 `_signal_reach_check`
    # 규약과 동일한 이유). 인라인 복제도 함께 없앤다 — 그 사본에는 2026-08-03에 `legs_from_chain_rows()`
    # 에 넣은 만기 경과 레그 배제가 반영돼 있지 않았다(지금은 `_chain_snapshot()`의 SQL이 이미
    # 걸러 결과가 같지만, 규약이 어긋난 사본을 남겨둘 이유가 없다).
    legs, gex_expiry = signal_book_legs(chain_rows, today)

    # GEX 막대도 `legs`와 같은 북만 — 막대와 감마플립/감마월이 다른 체인에서 나오면 안 된다.
    by_strike: dict[float, float] = {}
    for row in chain_rows:
        if gex_expiry is None or row.get("expiry") != gex_expiry:
            continue
        by_strike[row["strike"]] = by_strike.get(row["strike"], 0.0) + row["gex"]
    chain = [ChainPoint(strike=s, gex=g) for s, g in sorted(by_strike.items())]

    if investor_flow is not None:
        foreign_net, institution_net, individual_net = investor_flow
    else:
        foreign_net, institution_net, individual_net = 0.0, 0.0, 0.0

    return DashboardSnapshot(
        as_of=ts,
        is_live=True,
        regime=RegimeLabel(regime_idx),
        regime_prob=regime_prob,
        higher_tf_regime=RegimeLabel(higher_tf_idx) if higher_tf_idx is not None else None,
        stability_flag=bool(stability_flag),
        # NULL은 bool()로 뭉개지 않는다 — "모른다"와 "학습된 판정이다"는 다르다.
        regime_is_warmup=bool(is_warmup) if is_warmup is not None else None,
        spot=spot,
        spot_asof=spot_asof,
        chain=chain,
        gamma_flip=find_gamma_flip(legs, spot) if legs else None,
        gamma_walls=_gamma_wall_strikes(legs, spot),
        gex_expiry=gex_expiry,
        futures_flow_symbol=futures_flow_symbol,
        timestamps=[row[0] for row in futures_rows],
        ofi_series=[float(row[2]) for row in futures_rows],
        vpin_series=[float(row[4]) if row[4] is not None else 0.0 for row in futures_rows],
        price_series=[float(row[1]) for row in futures_rows],
        microprice_series=[float(row[3]) for row in futures_rows],
        option_flow_symbol=option_flow_symbol,
        option_timestamps=[row[0] for row in option_rows],
        option_ofi_series=[float(row[2]) for row in option_rows],
        option_vpin_series=[float(row[4]) if row[4] is not None else 0.0 for row in option_rows],
        option_price_series=[float(row[1]) for row in option_rows],
        option_microprice_series=[float(row[3]) for row in option_rows],
        foreign_net=foreign_net,
        institution_net=institution_net,
        individual_net=individual_net,
        expiry_liquidity=expiry_liquidity,
        macro_snapshot=macro_snapshot,
    )


def _synthetic_macro_snapshot(rng: np.random.Generator) -> dict:
    vix_front = float(abs(rng.normal(18.0, 3.0)))
    vix_next = float(vix_front + rng.normal(0.3, 1.0))  # 평상시엔 살짝 콘탱고가 흔함
    return {
        "vix_front": vix_front,
        "vix_next": vix_next,
        "vix_term_structure": vix_next / vix_front - 1,
        "usdcnh": float(7.05 + rng.normal(0, 0.05)),
        "us10y_yield": float(4.3 + rng.normal(0, 0.15)),
        "zn_front": float(110.0 + rng.normal(0, 0.5)),
    }


def _synthetic_snapshot(seed: int | None = None) -> DashboardSnapshot:
    rng = np.random.default_rng(seed)
    now = datetime.now()  # DB에 안 쓰이는 순수 합성 더미 시각이라 db.local_now() 정책 대상 아님
    n = 60
    timestamps = [now - timedelta(minutes=n - i) for i in range(n)]

    spot = 350.0 + np.cumsum(rng.normal(0, 0.15, n))
    ofi_series = rng.normal(0, 300, n).cumsum() * 0.05
    vpin_series = np.clip(0.3 + rng.normal(0, 0.15, n).cumsum() * 0.02, 0.05, 0.95)
    microprice_series = spot + rng.normal(0, 0.05, n)

    # 옵션 계열은 선물과 스케일이 다르다(체결가가 지수 포인트가 아니라 옵션 프리미엄) — 별도로 합성.
    option_price = 50.0 + np.cumsum(rng.normal(0, 0.2, n))
    option_ofi_series = rng.normal(0, 50, n).cumsum() * 0.05
    option_vpin_series = np.clip(0.3 + rng.normal(0, 0.15, n).cumsum() * 0.02, 0.05, 0.95)
    option_microprice_series = option_price + rng.normal(0, 0.05, n)

    strikes = [340 + 2.5 * i for i in range(9)]
    chain = [ChainPoint(strike=s, gex=float(rng.normal(0, 1) * (1 if s < spot[-1] else -1) * 5e8)) for s in strikes]
    # 합성 체인은 북이 하나뿐이라는 전제 — 라이브와 같은 형태를 유지하려고 만기를 하나 지어낸다
    # (합성 모드는 이미 `is_live=False` 배너로 "가짜 데이터"임을 명시한다).
    synthetic_gex_expiry = (now + timedelta(days=23)).date()

    regime_prob = {r: 0.0 for r in RegimeLabel}
    dominant = rng.choice(list(RegimeLabel))
    remaining = [r for r in RegimeLabel if r != dominant]
    regime_prob[dominant] = 0.62
    leftover_share = 0.38 / len(remaining)
    for r in remaining:
        regime_prob[r] = leftover_share

    return DashboardSnapshot(
        as_of=now,
        is_live=False,
        regime=dominant,
        regime_prob=regime_prob,
        higher_tf_regime=None,
        stability_flag=regime_prob[dominant] >= 0.4,
        regime_is_warmup=False,  # 합성은 확률이 퍼진 형태라 학습된 판정을 흉내 낸다
        spot=float(spot[-1]),
        spot_asof=timestamps[-1],
        chain=chain,
        gamma_flip=float(spot[-1] - rng.uniform(-5, 5)),
        gamma_walls=[strikes[6]],  # 라이브와 동일하게 1개(엔진 top_n=1)
        gex_expiry=synthetic_gex_expiry,
        futures_flow_symbol=None,
        timestamps=timestamps,
        ofi_series=list(ofi_series),
        vpin_series=list(vpin_series),
        price_series=list(spot),
        microprice_series=list(microprice_series),
        option_flow_symbol="SYNTH_OPT",
        option_timestamps=timestamps,
        option_ofi_series=list(option_ofi_series),
        option_vpin_series=list(option_vpin_series),
        option_price_series=list(option_price),
        option_microprice_series=list(option_microprice_series),
        foreign_net=float(rng.normal(0, 300)),
        institution_net=float(rng.normal(0, 200)),
        individual_net=float(rng.normal(0, 250)),
        expiry_liquidity=[
            {
                "series": "regular",
                "expiry": synthetic_gex_expiry,
                "atm_spread_pct": float(abs(rng.normal(0.04, 0.01))),
                "depth": float(abs(rng.normal(200, 40))),
                "volume": float(abs(rng.normal(500, 100))),
                "days_to_expiry": 23,
            },
            {
                "series": "weekly_mon",
                "expiry": (now + timedelta(days=2)).date(),
                "atm_spread_pct": float(abs(rng.normal(0.09, 0.02))),
                "depth": float(abs(rng.normal(80, 20))),
                "volume": float(abs(rng.normal(150, 40))),
                "days_to_expiry": 2,
            },
            {
                "series": "weekly_thu",
                "expiry": (now + timedelta(days=5)).date(),
                "atm_spread_pct": float(abs(rng.normal(0.10, 0.02))),
                "depth": float(abs(rng.normal(75, 20))),
                "volume": float(abs(rng.normal(140, 40))),
                "days_to_expiry": 5,
            },
        ],
        macro_snapshot=_synthetic_macro_snapshot(rng),
    )


def record_cockpit_startup() -> str:
    """
    계산: COCKPIT_START_MARKER_FILE에 남아있는 직전 COCKPIT 기동 시각과 현재 시각의 차이를
         메시지 문자열로 만들어 반환한 뒤, 이번 기동 시각으로 마커를 갱신한다 — 관측 루프의
         mahdi.main._log_startup_gap_since_last_run()과 동일한 패턴(2026-07-22 도입).
    해석: 문자열을 반환만 하고 로깅은 호출측(app.py)이 print()로 한다 — 이 프로세스(Streamlit)는
         logging 핸들러가 설정돼 있지 않아 logger.info를 써도 cockpit.log에 실제로는 안 남는다
         (Streamlit 자체 stdout/traceback만 배치스크립트의 리다이렉트로 남는 상태).
    실패 조건: 마커 파일이 없으면(최초 실행) 또는 파싱 실패하면 비교 없이 안내 메시지만 반환하고,
              그래도 마커 갱신은 시도한다. 마커 읽기/쓰기 자체가 실패해도(권한 등) 예외를 삼킨다
              — 이 기능 하나 때문에 COCKPIT 기동 전체가 죽으면 안 된다.
    """
    try:
        if COCKPIT_START_MARKER_FILE.exists():
            last = datetime.fromisoformat(COCKPIT_START_MARKER_FILE.read_text(encoding="utf-8").strip())
            gap_hours = (db.local_now() - last).total_seconds() / 3600
            message = f"직전 COCKPIT 기동: {last:%Y-%m-%d %H:%M:%S} ({gap_hours:.1f}시간 전)"
        else:
            message = "직전 COCKPIT 기동 기록 없음(최초 실행 또는 마커 파일 삭제됨)"
    except Exception:
        message = "직전 COCKPIT 기동 기록 확인 실패"

    try:
        COCKPIT_START_MARKER_FILE.parent.mkdir(parents=True, exist_ok=True)
        COCKPIT_START_MARKER_FILE.write_text(db.local_now().isoformat(), encoding="utf-8")
    except Exception:
        pass

    return message
