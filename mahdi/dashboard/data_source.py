"""COCKPIT 데이터 소스 — DB 우선 조회, 실패/데이터 없음 시 합성 리플레이로 폴백.

폴백이 있는 이유: 대시보드는 실시간 수집 파이프라인이 아직 안 돌고 있어도(또는 장 종료 후에도)
독립 실행 가능해야 관측 인프라 검증에 쓸모가 있다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta

import numpy as np

from mahdi.data import db
from mahdi.engines.regime import RegimeLabel
from mahdi.engines.regime_pipeline import FEATURE_VERSION
from mahdi.features.options_intel import OptionLeg, find_gamma_flip, gamma_walls as compute_gamma_walls

logger = logging.getLogger("mahdi.dashboard.data_source")

# 심볼 혼입 버그(2026-07-06) 시기에 쓰던 옛 고정 라벨 — 더 이상 아무도 안 쓰지만 남아있는
# 화석 데이터라 Flow Radar "가장 활발한 종목" 선정에서 제외한다.
_LEGACY_MIXED_SYMBOL = "KOSPI200_OPT"

# Flow Radar "가장 활발한 옵션" 선정 룩백 윈도 — 2026-07-06 위클리 북 추가 후 실측: 여러 위클리
# 종목이 같은 1분봉 timestamp로 동시에 찍혀("ORDER BY max(timestamp) DESC"에 동률), COCKPIT이
# 10초마다 리런될 때마다 임의로 다른 종목이 뽑혀 차트가 매번 완전히 다른 종목(다른 가격대)으로
# 바뀌어 보이는 문제가 실제로 발생함. "가장 최근 틱 1개"가 아니라 "최근 N분간 누적거래량"으로
# 기준을 바꿔, 단일 틱의 우연한 타이밍이 아니라 실제 상대적 활발함이 선정을 좌우하게 한다.
FLOW_RADAR_OPTION_LOOKBACK_MINUTES = 10


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
    spot: float
    chain: list[ChainPoint]
    gamma_flip: float | None
    gamma_walls: list[float]
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


# 2026-07-19(§5-6 "오늘의 점검 요약") — 1-B 장중 체크리스트의 "결손 여부" 기준(§5-4 Slack 알림의
# OPTION_CHAIN_GAP_ALERT_SECONDS와 동일한 5분)과 정규장 시간. 공휴일 캘린더는 없음(이 코드베이스
# 어디에도 아직 없음 — 평일 09:00~15:45만 "장중"으로 본다).
_TRADING_DAY_START = dtime(9, 0)
_TRADING_DAY_END = dtime(15, 45)
_STALE_DATA_THRESHOLD_SECONDS = 300.0


def _is_trading_hours(now: datetime) -> bool:
    return now.weekday() < 5 and _TRADING_DAY_START <= now.time() <= _TRADING_DAY_END


def _freshness_check(label: str, latest_ts: datetime | None, now: datetime) -> HealthCheck:
    """장중이 아니면(주말/장외시간) 데이터가 안 들어와도 정상이므로 판단하지 않는다 — 장중에만
    §5-4와 동일한 5분 기준으로 결손 여부를 판단한다.

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
    if latest_ts.tzinfo is not None:
        latest_ts = latest_ts.replace(tzinfo=None)
    age_seconds = max((now - latest_ts).total_seconds(), 0.0)
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
    return _freshness_check(label, latest_ts, now)


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
        eta_detail = f"약 {remaining_rows / avg_rows_per_day:.0f}영업일 남음(하루 평균 {avg_rows_per_day:.0f}행 기준 추정)"
    else:
        eta_detail = "누적 속도 계산 불가"
    return HealthCheck(
        label, "info",
        f"{total_rows:,}/{_REGIME_FIT_TARGET_ROWS:,}행 ({distinct_days}/{_REGIME_FIT_TARGET_BUSINESS_DAYS}영업일) — {eta_detail}",
    )


def get_health_summary(underlying: str = "KOSPI200") -> list[HealthCheck]:
    """
    입력: 기초자산 라벨.
    계산: 운영점검보고서 §1-B 장중 체크리스트 중 SQL로 자동화 가능한 항목들(§5-6 "오늘의 점검
         요약") — 옵션체인/선물 데이터 결손, 옵션체인 콜/풋 균형(2026-07-20 추가), CBOT 승인
         상태, series/symbol 화석 데이터 잔존 여부, 오늘 레짐 stability_flag 비율, feature_store
         20영업일 목표 진행률(§5-7) — 을 매번 사람이 DB를 직접 조회하지 않고 COCKPIT 상단에서
         바로 볼 수 있게 한다.
    실패 조건: 항목별로 독립적으로 조회한다 — 하나가 실패해도(쿼리 오류 등) rollback 후 나머지
              항목은 계속 보여준다. DB 연결 자체가 안 되면 단일 "조회 불가" 항목 하나만 반환한다.
    """
    try:
        with db.get_connection() as conn:
            now = db.local_now()
            return [
                _option_chain_freshness_check(conn, underlying, now),
                _futures_freshness_check(conn, underlying, now),
                _option_chain_leg_balance_check(conn, underlying, now),
                _cbot_status_check(conn),
                _fossil_data_check(conn, underlying, now),
                _regime_stability_check(conn, now),
                _regime_fit_progress_check(conn, underlying),
            ]
    except Exception:
        logger.warning("점검 요약 조회 실패", exc_info=True)
        return [HealthCheck("오늘의 점검 요약", "warning", "DB 연결 실패로 조회 불가")]


def load_snapshot(underlying: str = "KOSPI200") -> DashboardSnapshot:
    live = _load_from_db(underlying)
    return live if live is not None else _synthetic_snapshot()


def _load_from_db(underlying: str) -> DashboardSnapshot | None:
    try:
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT timestamp, regime, prob_vector, higher_tf_regime, stability_flag "
                    "FROM regime_state ORDER BY timestamp DESC LIMIT 1"
                )
                regime_row = cur.fetchone()
                if regime_row is None:
                    return None
                # 리런 시각(datetime.now())이 아니라 스냅샷 자체의 최신 시각을 룩백 기준으로 쓴다 —
                # 장 마감 후 리플레이/재현 시나리오에서도 "최근 N분"이 항상 실제 데이터 시각 기준으로
                # 맞아야 하기 때문(datetime.now() 기준이면 지난 데이터를 볼 때 항상 윈도가 텅 빈다).
                as_of_ts = regime_row[0]

            spot = db.latest_underlying_spot(conn, underlying)
            if spot is None:
                return None

            chain_rows = db.latest_option_chain(conn, underlying)
            investor_flow = db.latest_investor_flow(conn, underlying)
            expiry_liquidity = db.latest_expiry_liquidity(conn, underlying)
            # 매크로 폴러(poll_macro_snapshot)는 다른 폴러들과 별개 실패 도메인(해외선물옵션
            # 계좌 제약 등)이라, 이 조회 하나가 실패해도 대시보드 전체가 합성 폴백으로 떨어지면
            # 안 된다 — 독립적으로 감싸 None으로만 처리한다.
            try:
                macro_snapshot = db.latest_macro_snapshot(conn)
            except Exception:
                logger.warning("매크로 스냅샷 조회 실패", exc_info=True)
                macro_snapshot = None

            # 선물 계열: active_futures_symbol 레지스트리로 현재 구독 중인 선물 단축코드를
            # 명시적으로 조회한다(vpin 유무 같은 휴리스틱에 더 이상 의존하지 않음 — 2026-07-06,
            # 옵션에도 VPIN을 적용하면서 그 휴리스틱이 깨졌기 때문).
            futures_flow_symbol = db.get_active_futures_symbol(conn, underlying)

            with conn.cursor() as cur:
                futures_rows: list = []
                if futures_flow_symbol is not None:
                    cur.execute(
                        "SELECT timestamp, close, ofi, microprice, vpin FROM market_raw_1m "
                        "WHERE symbol=%s ORDER BY timestamp DESC LIMIT 60",
                        (futures_flow_symbol,),
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
                        "WHERE symbol=%s ORDER BY timestamp DESC LIMIT 60",
                        (option_flow_symbol,),
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
    ts, regime_idx, prob_vector, higher_tf_idx, stability_flag = regime_row
    regime_prob = {RegimeLabel(i): float(p) for i, p in enumerate(prob_vector)}

    today = db.local_now().date()
    legs = [
        OptionLeg(
            strike=row["strike"],
            option_type=row["option_type"].lower(),
            oi=row["oi"],
            iv=row["iv"],
            t_years=max((row["expiry"] - today).days, 0) / 365.0,
            gamma=row["gamma"],
        )
        for row in chain_rows
        if row["expiry"] is not None
    ]

    by_strike: dict[float, float] = {}
    for row in chain_rows:
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
        spot=spot,
        chain=chain,
        gamma_flip=find_gamma_flip(legs, spot) if legs else None,
        gamma_walls=[strike for strike, _ in compute_gamma_walls(legs, spot)] if legs else [],
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
        spot=float(spot[-1]),
        chain=chain,
        gamma_flip=float(spot[-1] - rng.uniform(-5, 5)),
        gamma_walls=[strikes[2], strikes[6]],
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
                "expiry": (now + timedelta(days=23)).date(),
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
