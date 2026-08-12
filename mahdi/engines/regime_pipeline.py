"""E1 Regime 실시간 오케스트레이션 — §7.3 피처 축적 + §7.4/§16.1 워밍업 실데이터화 + HMM 전환.

main.py는 매 선물봉마다 RegimeStateMachine.step()만 호출하면 된다. 내부적으로:
  1) 세션 내 인메모리 롤링 윈도(고/저/종가, ATM IV, 스프레드)로 6개 피처를 계산해 feature_store에
     매분 적재한다(오프라인 fit 배치의 원료 축적 — scripts/fit_regime_engine.py).
  2) data/models/regime_engine.pkl에 캘리브레이션된 모델이 있고 세션 내 워밍업(burn-in)이 끝났으면
     RegimeEngine.predict()를, 아니면 실거래 데이터로 계산한 gap_zscore/macro_score/전일 마감
     레짐을 넣은 warmup_fallback()을 반환한다.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from mahdi.data import db
from mahdi.engines.regime import FEATURE_NAMES, RegimeEngine, RegimeLabel, RegimeState, warmup_fallback
from mahdi.features.regime_features import adx, book_thinning, cross_asset_stress, hurst_exponent, iv_change_rate, rv_ratio

# 2026-08-03(운영점검보고서 §5-2) — 판단 축의 관측. 상세 근거는 `mahdi/fusion/engine.py`의 logger
# 주석 참고. 여기서는 **피처가 중립값을 처음 벗어나는 순간**만 남긴다(피처당 평생 1건).
#
# 왜 이 순간인가: `rv_ratio`는 유효 종가가 21일 미만이면 중립값 1.0을 반환하는데(정상 동작),
# 07-30에 그 사실을 알아내는 데 `feature_store` 전체 5,394행을 뒤져야 했다. 실제로 살아나는
# 시점이 로그에 한 줄 남으면 그 조사 자체가 필요 없다 — 그리고 **예상일이 지나도 그 줄이 안
# 나오면 그게 곧 이상 신호**다(08-04 예상, 2026-08-03 §2-6).
logger = logging.getLogger("mahdi.engines.regime_pipeline")

# 피처별 "아직 살아있지 않다"를 뜻하는 값 — `mahdi/ops/db_metrics.py`의 `_FEATURE_NEUTRAL`과
# 같은 정의다(리포트 지표와 로그가 다른 기준을 쓰면 안 된다).
_FEATURE_NEUTRAL_VALUES = {"rv_ratio": 1.0, "book_thinning": 0.0, "cross_asset_stress": 0.0}

if TYPE_CHECKING:
    from mahdi.data.collector import MinuteBar

FEATURE_VERSION = "v1"
DEFAULT_MODEL_PATH = Path("data/models/regime_engine.pkl")

_ROLLING_WINDOW_MINUTES = 120  # Hurst/ADX 입력 — 약 2시간, R/S 방법이 안정적으로 수렴하는 최소 길이
_IV_WINDOW_MINUTES = 30
_SPREAD_WINDOW_MINUTES = 30
_MIN_WARMUP_BARS = 30  # 이 정도 봉이 쌓이기 전에는 모델이 있어도 predict() 대신 warmup_fallback 유지(burn-in)

# 2026-08-10 — `RegimeEngine.predict()`에 넘기는 **세션 누적 창**의 길이.
#
# `_ROLLING_WINDOW_MINUTES`와 값이 같지만 **독립 상수로 선언한다**: 저쪽은 Hurst/ADX가 수렴하는
# 데 필요한 관측 창이고 이쪽은 HMM 전방 필터링의 문맥 길이다 — 서로 다른 결정이며, 한쪽을 바꿀
# 때 다른 쪽이 조용히 따라가면 안 된다.
#
# **왜 창이어야 하는가**: 종전 구현은 `predict(np.array([features]))`로 **길이 1**을 넘겼다.
# 길이 1의 사후확률은 `normalize(startprob ⊙ emission(x))`라 전이행렬이 통째로 안 쓰이고,
# `startprob_`가 뾰족하면 출력이 입력과 무관한 상수가 된다. 08-10 재학습 모델이 정확히 그랬고
# (`startprob_` 비영 1개), 전 이력 8,241분이 전부 TREND_UP_STRONG으로 나왔다.
# 같은 모델에 창을 넘기면 8종이 나온다 — **모델에는 정보가 있었고 호출 방식이 그것을 버렸다.**
#
# 대가: 상태 전환이 는다(전 이력 리플레이 실측 길이1 5.1%/분 → 창 12.9%/분). 그 민감도는
# `RegimeState.stability_flag`(최고확률 < 0.40 → REGIME_UNSTABLE)가 흡수하도록 설계돼 있다.
_PREDICT_WINDOW_MINUTES = 120

# 예측 창에 넣기 전에 거르는 피처 절대값 상한 — `scripts/fit_regime_engine.py`의
# `_MAX_ABS_FEATURE_VALUE`와 **같은 값이어야 한다**. 학습이 걸러낸 종류의 값을 예측이 먹으면
# 라이브가 학습 분포 밖에서 돈다(08-03에 cross_asset_stress 1e11이 EM을 발산시킨 그 값들이다).
_MAX_ABS_PREDICT_FEATURE = 100.0
_DAILY_CLOSES_LOOKBACK_DAYS = 30  # rv_ratio가 21개를 요구 — 롤오버 등을 감안해 여유 있게 조회
_MACRO_STRESS_DAILY_LOOKBACK_DAYS = 10  # USDKRW/US10Y(일봉 전용) z-score 베이스라인 — 9거래일치 baseline
_MACRO_STRESS_USDCNH_RECENT_BUCKETS = 24  # USDCNH(5분 주기) z-score 베이스라인 — 약 2시간
_MACRO_SCORE_DAILY_LOOKBACK_DAYS = 5  # macro_score의 USDKRW 추세 판단 — 최근 며칠 방향이면 충분
_MACRO_SCORE_RECENT_BUCKETS = 12  # macro_score의 USDCNH/ES 추세 판단(5분 주기) — 약 1시간

# 2026-08-05(COCKPIT 육안 점검 P1-4) — 신호 경로에서 매크로 스냅샷에 허용하는 최대 나이.
# 폴링 주기가 5분(`main.MACRO_SNAPSHOT_POLL_INTERVAL_SECONDS=300`)이므로 15분은 **연속 2회 실패를
# 견디고 3회째에 끊는** 값이다. 이보다 짧으면 한 번의 조회 실패로 VIX 신호가 사라져 스코어의
# 분모가 흔들리고, 길게 잡으면 "지금 시장"이라 부를 수 없는 값이 판단에 들어간다.
MACRO_SNAPSHOT_MAX_AGE_MINUTES = 15


class RegimeFeatureBuilder:
    """선물 1분봉 롤링 윈도로 §7.3 6개 피처를 계산한다."""

    def __init__(
        self,
        window: int = _ROLLING_WINDOW_MINUTES,
        iv_window: int = _IV_WINDOW_MINUTES,
        spread_window: int = _SPREAD_WINDOW_MINUTES,
    ) -> None:
        self._closes: deque[float] = deque(maxlen=window)
        self._highs: deque[float] = deque(maxlen=window)
        self._lows: deque[float] = deque(maxlen=window)
        self._spreads: deque[float] = deque(maxlen=spread_window)
        self._ivs: deque[float] = deque(maxlen=iv_window)

    def update_bar(self, bar: "MinuteBar") -> None:
        """입력: 완성된 선물 1분봉. 계산: 고/저/종가/스프레드 롤링 윈도에 추가."""
        self._closes.append(bar.close)
        self._highs.append(bar.high)
        self._lows.append(bar.low)
        self._spreads.append(bar.bid_ask_spread)

    def update_iv(self, atm_iv: float) -> None:
        """입력: 옵션체인 폴링 사이클에서 뽑은 ATM 근사 IV(콜/풋 평균). 계산: IV 롤링 윈도에 추가."""
        self._ivs.append(atm_iv)

    def build(
        self,
        daily_closes: list[float],
        usdkrw_daily_series: list[float] = (),
        usdcnh_recent_series: list[float] = (),
        us10y_daily_series: list[float] = (),
    ) -> list[float]:
        """
        입력: 일별 종가 이력(rv_ratio용), Cross-asset stress 세 시퀀스(usdkrw_daily_series/
             us10y_daily_series는 거래일 단위, usdcnh_recent_series는 5분 스냅샷 단위 — 호출측
             (RegimeStateMachine.step)이 DB에서 매번 조회해 전달한다).
        계산: FEATURE_NAMES 순서(hurst, adx, rv_ratio, iv_chg, cross_asset_stress, book_thinning)로
             피처 벡터를 구성한다.
        """
        return [
            hurst_exponent(list(self._closes)),
            adx(list(self._highs), list(self._lows), list(self._closes)),
            rv_ratio(daily_closes),
            iv_change_rate(list(self._ivs)),
            cross_asset_stress(usdkrw_daily_series, usdcnh_recent_series, us10y_daily_series),
            book_thinning(list(self._spreads)),
        ]


def compute_gap_zscore(conn, underlying: str) -> float:
    """
    §16.1 WARMUP ② — 갭 z-score = (오늘 첫 스팟 − 전일 마지막 스팟) / 전일 ATM 스트래들 IV 기반
    오버나이트 기대변동폭.

    입력: DB 커넥션, underlying 라벨(예: "KOSPI200").
    계산: underlying_spot_1m에서 전일 마지막 스팟·오늘 첫 스팟을 조회하고, option_analysis_1m에서
         전일 마지막 시점 기준 그 스팟에 가장 가까운 행사가의 IV(콜/풋 평균)로 1캘린더데이 기대
         변동폭(spot·iv·sqrt(1/365))을 근사한다.
    실패 조건: 전일 데이터가 없거나(첫 실행일) IV를 못 찾으면 0.0(갭 없음으로 간주 — 안전한 중립값).
    """
    today = db.local_now().date()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT timestamp, spot FROM underlying_spot_1m WHERE underlying=%s AND timestamp::date < %s "
            "ORDER BY timestamp DESC LIMIT 1",
            (underlying, today),
        )
        prev_row = cur.fetchone()
        cur.execute(
            "SELECT spot FROM underlying_spot_1m WHERE underlying=%s AND timestamp::date = %s "
            "ORDER BY timestamp ASC LIMIT 1",
            (underlying, today),
        )
        today_row = cur.fetchone()

    if prev_row is None or today_row is None:
        return 0.0

    prev_ts, prev_close = prev_row
    prev_close = float(prev_close)
    today_open = float(today_row[0])
    if prev_close <= 0:
        return 0.0

    with conn.cursor() as cur:
        cur.execute(
            "SELECT iv FROM option_analysis_1m WHERE underlying=%s AND timestamp::date=%s AND iv IS NOT NULL "
            "ORDER BY ABS(strike - %s) ASC, timestamp DESC LIMIT 2",
            (underlying, prev_ts.date(), prev_close),
        )
        iv_rows = cur.fetchall()

    if not iv_rows:
        return 0.0
    atm_iv = sum(float(r[0]) for r in iv_rows) / len(iv_rows)
    expected_move = prev_close * atm_iv * math.sqrt(1 / 365)
    if expected_move <= 0:
        return 0.0
    return (today_open - prev_close) / expected_move


def _directional_sign(value: float) -> float:
    if value > 0:
        return 1.0
    if value < 0:
        return -1.0
    return 0.0


def _trend_sign(series: list[float]) -> float | None:
    """
    계산: 시퀀스 마지막 값이 그 앞(직전 구간) 평균보다 높으면 +1, 낮으면 -1.
    실패 조건: 데이터 2개 미만이거나 마지막 값이 평균과 같으면(추세 없음) None — 신호 자체가
              없는 것으로 취급해 호출측이 평균 계산에서 제외하게 한다.
    """
    if len(series) < 2:
        return None
    baseline = series[:-1]
    mean = sum(baseline) / len(baseline)
    if series[-1] == mean:
        return None
    return 1.0 if series[-1] > mean else -1.0


def compute_macro_score_proxy(conn, underlying: str) -> float:
    """
    §16.1 WARMUP ①의 "장전 매크로 스코어" — 위험선호(+)/위험회피(-) 복합 신호.

    2026-07-20([[DECISION_LOG]] 2026-07-10 항목 "함수 시그니처는 유지하고 내부 구현만 바꿀 것"에
    따라 이름·시그니처는 유지하고 내부만 실데이터로 교체): 기존 외국인 순매수 부호(K-market 수급
    신호)에 VIX 기간구조·USDKRW·USDCNH·S&P500 선물(ES) 추세 신호를 추가한다. US10Y·MOVE는
    방향이 위험선호/회피로 명확히 매핑되지 않아(수익률·변동성 급등이 맥락에 따라 risk-off·
    risk-on 둘 다일 수 있음) 이 스코어에는 넣지 않는다 — 그 "급변 크기" 자체는 이미
    cross_asset_stress()가 별도로 포착한다.
    계산: 각 신호를 -1(위험회피)/0(중립)/+1(위험선호)로 정규화해 "존재하는 신호만" 평균 낸다
         (데이터가 없는 신호를 조용히 0.0으로 채우면 있는 신호가 희석되므로, 분모에서도 뺀다).
         - 외국인 순매수(foreign_net) 부호 그대로.
         - VIX 기간구조: 백워데이션(음수)=단기 스트레스 급등=위험회피, 콘탱고(양수)=위험선호.
         - USDKRW/USDCNH 추세: 최근 값이 직전 구간 평균보다 높으면(원화·위안화 약세) 위험회피로
           뒤집어 반영(-_trend_sign).
         - ES(S&P500 선물) 추세: 최근 값이 직전 구간 평균보다 높으면 위험선호로 그대로 반영.
    실패 조건: 신호가 하나도 없으면(투자자수급·매크로 스냅샷 모두 미폴링) 0.0(완전 중립).
    """
    signals: list[float] = []

    flow = db.latest_investor_flow(conn, underlying)
    if flow is not None:
        foreign_net, _institution_net, _individual_net = flow
        signals.append(_directional_sign(foreign_net))

    # 2026-08-05(COCKPIT 육안 점검 P1-4) — **신호 경로에서는 신선도 경계를 켠다.**
    # `latest_macro_snapshot()`은 종전에 시각 조건이 전혀 없어, 폴러가 며칠 죽어 있어도 그때의
    # VIX 기간구조 부호가 그대로 이 스코어에 들어왔다(그리고 그 사실을 볼 방법이 없었다).
    # 경계는 여기서만 켜고 COCKPIT은 끈 채 값+시각을 함께 표시한다 —
    # `latest_underlying_spot()`에서 정한 것과 같은 분업이다(그 docstring 참고).
    snapshot = db.latest_macro_snapshot(conn, max_age_minutes=MACRO_SNAPSHOT_MAX_AGE_MINUTES)
    if snapshot is not None and snapshot.get("vix_term_structure") is not None:
        signals.append(_directional_sign(snapshot["vix_term_structure"]))

    usdkrw_trend = _trend_sign(db.recent_usdkrw_daily_series(conn, days=_MACRO_SCORE_DAILY_LOOKBACK_DAYS))
    if usdkrw_trend is not None:
        signals.append(-usdkrw_trend)

    usdcnh_trend = _trend_sign(db.recent_usdcnh_series(conn, limit=_MACRO_SCORE_RECENT_BUCKETS))
    if usdcnh_trend is not None:
        signals.append(-usdcnh_trend)

    es_trend = _trend_sign(db.recent_es_front_series(conn, limit=_MACRO_SCORE_RECENT_BUCKETS))
    if es_trend is not None:
        signals.append(es_trend)

    if not signals:
        return 0.0
    return sum(signals) / len(signals)


def latest_prior_close_regime(conn) -> RegimeLabel:
    """전일 마감 레짐 조회 — 없으면(첫 실행일) RANGE_BALANCED로 폴백."""
    today_midnight = datetime.combine(db.local_now().date(), dtime.min)
    regime_int = db.latest_regime_before(conn, today_midnight)
    if regime_int is None:
        return RegimeLabel.RANGE_BALANCED
    return RegimeLabel(regime_int)


def replay_live_predictions(
    engine: RegimeEngine,
    sessions: list[np.ndarray],
    *,
    warmup: int = _MIN_WARMUP_BARS,
    window: int = _PREDICT_WINDOW_MINUTES,
) -> list[RegimeLabel]:
    """
    입력: 캘리브레이션된 엔진, **세션별로 나눈** 피처 배열 목록((n_i, 6) 각각).
    계산: `RegimeStateMachine.step()`과 **똑같은 방식**으로 — 세션 안에서 워밍업 `warmup`봉을
         건너뛰고 최대 `window`분 누적 창을 만들어 — 매분 예측하고 레짐 라벨을 모아 돌려준다.
    해석: 2026-08-10. 저장 게이트가 "이 모델이 라이브에서 상태를 바꾸는가"를 검사하려면 **라이브와
         같은 축으로** 재야 한다. 종전 게이트(`scripts/fit_regime_engine.py`)는 전 이력을 한
         시퀀스로 배치 Viterbi해서 "잠재상태 8/8 방문"을 확인했는데, 같은 모델을 라이브 축으로
         재면 **1/8**이었다 — 재는 축이 주장하는 축과 달라 통과시킨 것이다(운영점검 규약 F의
         코드판). 그래서 이 함수는 스크립트가 아니라 **라이브 호출부와 같은 모듈**에 산다:
         `step()`의 창 구성이 바뀌면 게이트도 같이 바뀌어야 하고, 파일이 다르면 그 동기화가
         사람의 기억에 의존하게 된다.
    실패 조건: 없음 — 예측할 분이 하나도 없으면(전 세션이 warmup 이하) 빈 목록.
    """
    labels: list[RegimeLabel] = []
    for session in sessions:
        # 라이브 경계와 정확히 맞춘다: `step()`은 봉 인덱스 i에서 `_bar_count == i+1`이고
        # `_bar_count >= warmup`일 때 예측하며, 창에는 0..i행(최대 `window`개)이 들어 있다.
        # 여기서 한 칸이라도 어긋나면 게이트가 라이브와 다른 것을 재게 된다.
        for i in range(warmup - 1, len(session)):
            start = max(0, i - window + 1)
            labels.append(engine.predict(session[start : i + 1]).regime)
    return labels


# ===== 2026-08-12 §2-4 / Fix#7 — 재기동이 레짐을 WARMUP으로 되돌린다 (레버, 기본 OFF) =====
#
# ## 08-12에 무슨 일이 있었는가
#
#     08:45  warmup 시작
#     09:31  predict 진입
#     10:14  ← 워치독 재기동. **warmup 복귀**
#     10:43  predict 재개
#
# **29분의 판단이 `regime_hmm` 없이 갔다.** 원인은 아래 `_predict_window`/`_bar_count`가
# in-memory라 프로세스와 함께 사라지는 것이다 — 클래스 docstring이 *"세션 경계 초기화는 프로세스
# 재기동이 담당한다"* 고 적어 둔 그 계약이, 계획에 없던 재기동에서는 손실이 된다.
#
# ## 복원할 재료는 DB에 이미 있다
#
# `step()`이 매분 `feature_store`에 그날 피처를 적재한다. 재기동 시 그 행들을 시간순으로 읽어
# 창을 다시 채우면 predict가 즉시 가능해진다.
#
# ## 왜 기본 OFF인가 — 검증이 먼저다
#
# 복원해도 `feature_builder`의 롤링 창(closes/highs/lows/spreads/ivs)은 비어 있다. 즉 재기동
# **직후의 몇 봉**은 연속 실행했을 때와 다른 피처 값을 낸다(그 자체는 종전에도 같았고, 그 값들은
# 이미 `feature_store`에 그대로 적재됐다). 바뀌는 것은 **그 값들 위에서 predict를 도느냐**다.
#
# 이 저장소가 08-10에 데인 지점이 정확히 여기다: **오프라인 재계산이 라이브와 갈라지는 분기.**
# 그래서 켜기 전에 오프라인 재생으로 「복원한 창의 예측 == 연속 실행의 예측」을 확인한다
# (`replay_live_predictions`가 그 비교의 기준이다).
#
# ## 켜는 법과 예측치 (숫자를 보기 전에 적는다)
#
#   값    `REGIME_RESTORE_SESSION_WINDOW = True`
#   주장  재기동이 있는 날의 warmup 재진입이 **29분 → 5분 이하**
#   참고  `regime_state`의 `is_warmup=false` 분 수 — 재기동이 없는 날에는 **안 바뀐다**
#         (이 경로는 그런 날 아예 안 탄다. 안 바뀌는 것으로 반증하지 말 것)
#   대가  재기동 직후 얇은 `feature_builder` 위에서 predict가 돈다 — 그 분들의 레짐이
#         연속 실행과 다를 수 있다. 감시는 `db.regime` 전이 횟수(채터링 회귀 = 08-10의 병)
REGIME_RESTORE_SESSION_WINDOW = False


class RegimeStateMachine:
    """세션 하나(프로세스 하나)당 1개 — main.py가 선물봉마다 step()을 호출한다."""

    def __init__(self, underlying: str, futures_symbol: str, model_path: str | Path = DEFAULT_MODEL_PATH) -> None:
        self.underlying = underlying
        self.futures_symbol = futures_symbol
        self.feature_builder = RegimeFeatureBuilder()
        self._bar_count = 0
        self._gap_zscore: float | None = None  # 세션 첫 계산값을 캐싱(갭은 장중 재계산 대상이 아님)
        self.last_state: RegimeState | None = None  # 다른 폴러(Signal Fusion)가 재계산 없이 참조
        # 이번 프로세스에서 이미 "중립값 탈출"을 알린 피처 — 피처당 한 번만 남긴다(§5-2).
        self._escaped_neutral: set[str] = set()
        self._last_regime: RegimeLabel | None = None
        # 2026-08-10 — `RegimeEngine.predict()`에 넘길 세션 누적 피처 창(위 `_PREDICT_WINDOW_MINUTES`
        # 주석 참고). 이 클래스는 **세션당 1개**(클래스 docstring 계약)라 세션 경계 초기화는
        # 프로세스 재기동이 담당한다 — 여기에 별도 리셋 훅을 두지 않는다.
        self._predict_window: deque[list[float]] = deque(maxlen=_PREDICT_WINDOW_MINUTES)
        try:
            self.engine: RegimeEngine | None = RegimeEngine.load(model_path)
        except FileNotFoundError:
            self.engine = None

    def restore_session_window(self, conn, today: date) -> int:
        """
        입력: DB 커넥션, 오늘 날짜.
        계산: 오늘 `feature_store`에 이미 적재된 행으로 `_predict_window`와 `_bar_count`를
             되살린다. 반환은 복원한 봉 수(0이면 복원할 것이 없었다 = 그날 첫 기동).
        해석: 상세 근거는 `REGIME_RESTORE_SESSION_WINDOW` 위 주석. **레버가 꺼져 있으면
             아무것도 하지 않고 0을 반환한다** — 호출측이 분기를 갖지 않게 하기 위해서다
             (분기가 호출측에 있으면 레버를 켤 때 그 자리를 다시 짜게 된다).
        실패 조건: 조회/파싱 실패는 로그만 남기고 0 — **복원에 실패했다고 관측이 멈추면 안 된다.**
                  그때는 종전과 완전히 같은 동작(warmup부터)이 된다.
        """
        if not REGIME_RESTORE_SESSION_WINDOW:
            return 0
        try:
            history = db.get_feature_history(conn, self.underlying, FEATURE_VERSION)
        except Exception:
            logger.warning("레짐 세션 창 복원 실패 — warmup부터 시작한다", exc_info=True)
            return 0
        restored = 0
        for timestamp, named in history:
            if timestamp.date() != today:
                continue
            # **`step()`과 같은 순서·같은 필터로 채운다.** 여기서 규칙이 갈리면 복원한 날과
            # 연속 실행한 날의 창이 달라지고, 그 분기가 08-10 사고의 구조다.
            try:
                features = [float(named[name]) for name in FEATURE_NAMES]
            except (KeyError, TypeError, ValueError):
                continue
            self._bar_count += 1
            restored += 1
            if all(math.isfinite(v) and abs(v) <= _MAX_ABS_PREDICT_FEATURE for v in features):
                self._predict_window.append(features)
        if restored:
            logger.info(
                "레짐 세션 창 복원: 오늘 %d봉(예측 창 %d분) — 재기동이 WARMUP을 되돌리는 것을 막는다"
                "(2026-08-12 Fix#7)",
                restored, len(self._predict_window),
            )
        return restored

    def update_bar(self, bar: "MinuteBar") -> None:
        self.feature_builder.update_bar(bar)
        self._bar_count += 1

    def update_iv(self, atm_iv: float) -> None:
        self.feature_builder.update_iv(atm_iv)

    def step(self, conn, timestamp: datetime) -> RegimeState:
        """
        입력: DB 커넥션, 이번 선물봉의 타임스탬프.
        계산: 피처 벡터를 계산해 feature_store에 적재한 뒤, 캘리브레이션된 모델이 있고 세션 내
             워밍업(_MIN_WARMUP_BARS)이 끝났으면 predict(), 아니면 실데이터 기반 warmup_fallback()을
             반환한다.
        """
        # 2026-07-30(운영점검보고서 §2-3/§4 Fix#5): 선물 종목코드 기준(db.daily_closes)에서 지수
        # 기준(db.underlying_daily_closes)으로 교체 — 선물은 분기 롤오버 때마다 종목코드가 바뀌어
        # 일별 이력이 0으로 리셋되고, 그동안 rv_ratio가 중립값 1.0에 고정된다(실측: feature_store
        # 전체 5,394건 중 rv_ratio != 1.0인 행 0건). 지수는 롤오버가 없다.
        daily_closes = db.underlying_daily_closes(conn, self.underlying, days=_DAILY_CLOSES_LOOKBACK_DAYS)
        usdkrw_daily_series = db.recent_usdkrw_daily_series(conn, days=_MACRO_STRESS_DAILY_LOOKBACK_DAYS)
        usdcnh_recent_series = db.recent_usdcnh_series(conn, limit=_MACRO_STRESS_USDCNH_RECENT_BUCKETS)
        us10y_daily_series = db.recent_us10y_daily_series(conn, days=_MACRO_STRESS_DAILY_LOOKBACK_DAYS)
        features = self.feature_builder.build(
            daily_closes, usdkrw_daily_series, usdcnh_recent_series, us10y_daily_series
        )
        named_features = dict(zip(FEATURE_NAMES, features))
        self._log_neutral_escapes(named_features)
        db.insert_feature_store(conn, timestamp, self.underlying, named_features, FEATURE_VERSION)

        # 2026-08-10 — 예측 창 적재. **적재는 feature_store 저장 뒤에 한다**: DB에는 그날 실제로
        # 계산된 값이 무엇이었든 남아야 하고(기록), 예측 창에는 학습이 받아들이는 범위의 값만
        # 들어가야 한다(판단). 두 목적이 달라 필터가 여기에만 걸린다.
        if all(math.isfinite(v) and abs(v) <= _MAX_ABS_PREDICT_FEATURE for v in features):
            self._predict_window.append(features)

        # `_predict_window`가 비어 있으면(이번 세션 전 행이 필터에 걸림) 모델이 있어도 못 부른다 —
        # 빈 배열을 넘기면 predict_proba가 IndexError를 낸다. 그때는 폴백이 정답이다.
        use_model = self.engine is not None and self._bar_count >= _MIN_WARMUP_BARS and bool(self._predict_window)
        if use_model:
            state = self.engine.predict(np.array(self._predict_window))
        else:
            if self._gap_zscore is None:
                self._gap_zscore = compute_gap_zscore(conn, self.underlying)
            macro_score = compute_macro_score_proxy(conn, self.underlying)
            prior_regime = latest_prior_close_regime(conn)
            state = warmup_fallback(prior_regime, macro_score=macro_score, gap_zscore=self._gap_zscore)

        if self._last_regime != state.regime:
            # 2026-08-10 — 모델/폴백 표기는 **실제로 탄 분기**(`use_model`)에서 가져온다. 종전에는
            # 조건식을 여기서 한 번 더 적었는데, 분기 조건이 늘면(예측 창 비어 있음) 그 복제가
            # 조용히 틀려 로그가 "predict"라고 쓰면서 폴백 값을 싣게 된다.
            logger.info(
                "레짐 전이: %s → %s (안정=%s, 모델=%s, 창=%d분)",
                self._last_regime.name if self._last_regime is not None else "(최초)",
                state.regime.name, state.stability_flag,
                "predict" if use_model else "warmup_fallback",
                len(self._predict_window),
            )
            self._last_regime = state.regime

        self.last_state = state
        return state

    def _log_neutral_escapes(self, named_features: dict[str, float]) -> None:
        """
        입력: 이번 봉의 피처 이름→값.
        계산: `_FEATURE_NEUTRAL_VALUES`에 등록된 피처가 **처음** 중립값을 벗어나면 INFO 한 줄.
        해석: 2026-08-03 §5-2 — 피처당 평생 1건이라 로그 볼륨 부담이 없고, **예상일이 지나도
             줄이 안 나오면 그 자체가 이상 신호**다(rv_ratio는 유효 종가 21일째인 08-04 예상).
        실패 조건: 없음.
        """
        for name, neutral in _FEATURE_NEUTRAL_VALUES.items():
            if name in self._escaped_neutral:
                continue
            value = named_features.get(name)
            if value is None or value == neutral:
                continue
            self._escaped_neutral.add(name)
            logger.info("피처 활성화: %s가 중립값(%s)을 처음 벗어났다 — 현재 %.6g", name, neutral, value)
