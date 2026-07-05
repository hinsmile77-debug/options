# PROJECT MAHDI — ULTIMATE INTEGRATED TRADING SYSTEM
## Version 5.0 · The Definitive Hedge Fund Blueprint
> **프로젝트 경로**: `C:\Users\82108\PycharmProjects\options`  
> **기반 기술**: Python 3.1x (64-bit) + 한국투자증권 OpenAPI+  

> "시장을 맞히려는 시스템이 아니라,  
> 구조적으로 유리할 때만 위험을 꺼내 쓰는 시스템.  
> 살아남는 것이 먼저다. 버는 것은 그 다음이다."

> Built on: Renaissance × Two Sigma × AQR × D.E. Shaw Intelligence  
> Calibrated for: KOSPI200 Options · Intraday Execution · Same-Day Risk Closure

---

## 0. VERSION HISTORY & WHAT'S NEW IN v5

| 버전 | 핵심 추가 내용 |
|------|----------------|
| v1 | 5대 엔진 기본 설계 |
| v2 | Volume Intelligence, 주체 분석, Regime Engine |
| v3 | HMM, Meta-Labeling, Kelly, Heston, 학술논문 기반 |
| v4 | 한국 옵션시장 실전 제약, 당일청산 원칙, Portfolio Greeks, Tail Risk |
| **v5** | **SABR 모델, Vanna-Volga, 고차 Greeks PnL Attribution, Avellaneda-Stoikov MM, Dispersion Trading, Variance Swap 프레임워크, 키움 API 상태머신, Greeks 실시간 대차대조표, 장중 VRP 포착 전술, CPPI 자본보호, Combinatorial Purged CV, 실전 Python 아키텍처** |

---

## 1. SYSTEM PHILOSOPHY — 최종 공리

```
Axiom 1.  Price is the shadow. Order flow is the cause.
Axiom 2.  옵션 시장은 스마트머니의 리스크 대차대조표다.
Axiom 3.  강한 신호보다 중요한 것은 그 신호가 어떤 레짐에서 발생했는가이다.
Axiom 4.  높은 기대수익보다 먼저 확보해야 할 것은 낮은 파산확률이다.
Axiom 5.  모든 진입은 가설이고, 청산은 검증이다.
Axiom 6.  좋은 전략은 승률보다 손익비, 손실 비대칭, 반복 가능성에서 드러난다.
Axiom 7.  당일 청산 원칙은 성과 제약이 아니라 생존 프리미엄이다.
Axiom 8.  Greeks는 포지션의 언어다. Delta를 모르면 포지션을 모르는 것이다.
Axiom 9.  변동성은 자산이다. 방향만 보는 트레이더는 절반의 시장만 보는 것이다.
Axiom 10. 시스템은 규칙이 아니라 진화하는 유기체여야 한다.
```

### 1.1 시스템 비유 지도

| 구성요소 | 비유 | 역할 |
|----------|------|------|
| Regime Engine | 날씨예보관 | 오늘 시장의 기후 판단 |
| Order Flow Engine | 법의학자 | 가격 움직임의 원인 해부 |
| Options Intelligence | 기압위성 | 폭풍 전 이미 구조를 본다 |
| Greeks PnL Engine | 재무 대차대조표 | 포지션의 실시간 리스크 회계 |
| Signal Fusion | 오케스트라 지휘자 | 각 악기 소리를 하모니로 |
| Meta-Labeling | 팩트체커 | 신호를 믿을지 말지 결정 |
| Risk Engine | 항공기 안전시스템 | 파일럿과 독립적으로 작동 |
| Execution Engine | 외과의사 | 최소 절개, 최대 정확도 |
| Learning Engine | 다윈 진화론 | 살아남은 전략만 다음 세대로 |

---

## 2. ACADEMIC FOUNDATION — 학술 기반 전체 지도

### 2.1 핵심 논문 매트릭스

| 분야 | 논문 | 핵심 기여 | v5 적용 |
|------|------|-----------|---------|
| 미시구조 | Kyle (1985) | 정보거래자 모델 | VPIN, 주문독성 |
| 미시구조 | Glosten-Milgrom (1985) | 스프레드 분해 | 호가 독성 측정 |
| 미시구조 | Avellaneda-Stoikov (2008) | 최적 마켓메이킹 | 장중 호가 전략 |
| 변동성 | Black-Scholes (1973) | 옵션 기본 가격 | 기준선 |
| 변동성 | Heston (1993) | 확률적 변동성 | IV Surface |
| 변동성 | SABR (Hagan et al. 2002) | 스마일 보간 | Skew 정밀 캘리브 |
| 변동성 | Dupire (1994) Local Vol | 결정론적 vol | 국소변동성 |
| 변동성 | Carr-Madan (1998) | 분산스왑 복제 | VRP 포착 |
| 변동성 | Carr-Wu (2009) | VRP 측정 | 옵션 매도 타이밍 |
| 감마 | Taleb (1997) Dynamic Hedging | 실전 Greeks | 감마 스캘핑 |
| 감마 | Derman (1999) | 스마일 이론 | Vanna-Volga 구조 |
| 레짐 | Hamilton (1989) HMM | 마르코프 전환 | 레짐 탐지 |
| 레짐 | Ang-Bekaert (2002) | 레짐 포트폴리오 | 전략 전환 |
| 실행 | Almgren-Chriss (2001) | 최적 집행 | 슬리피지 최소화 |
| 실행 | Kissell (2013) TCA | 거래비용 분석 | 실행품질 측정 |
| ML | Lopez de Prado (2018) | 금융 ML | Meta-Labeling |
| ML | Bailey-de Prado (2014) | DSR | 과최적화 탐지 |
| 포지션 | Kelly (1956) | 최적 베팅 | Quarter Kelly |
| 포지션 | Thorp (1969) | Kelly 실전 | 분수 Kelly |
| 리스크 | Qian (2005) | Risk Parity | 리스크 예산 |
| 리스크 | Black-Litterman (1990) | 뷰 통합 | 자본배분 |
| 꼬리위험 | Taleb (2007) Black Swan | 비선형 리스크 | Tail Hedge |
| 꼬리위험 | Carr-Wu (2003) | Variance Swap | VRP 구조화 |
| 팩터 | Fama-French (1993, 2015) | 팩터 모델 | 구조적 배경 |
| 행동 | Kahneman-Tversky (1979) | Prospect Theory | 행동 오버레이 |

---

## 3. MARKET SCOPE AND OPERATING CONSTRAINTS

### 3.1 대상 시장 및 데이터 계층

```
주시장:   KOSPI200 옵션 (한국거래소, 코스피200 기초자산)
보조시장: KOSPI200 선물, KOSPI 현물, 미니선물
글로벌:   VIX, VIX3M, MOVE Index, US 10Y, USDKRW, USDCNH
크로스:   S&P500 선물, 닛케이 선물, 항셍 선물
```

### 3.2 운영 제약 (한국 옵션시장 현실)

```python
OPERATING_CONSTRAINTS = {
    # 시간
    "primary_timeframe":    "1m",
    "aux_timeframes":       ["tick", "3m", "5m", "15m"],
    "session_open":         "09:00",
    "session_close":        "15:20",
    "forced_flat_by":       "15:10",       # 당일청산 하드룰
    "overnight_exposure":   False,          # 절대 금지

    # 한국 옵션시장 특성
    "liquidity_peak":       "09:00-10:30", # 유동성 집중 구간
    "lunch_vacuum":         "12:00-13:00", # 유동성 공백
    "expiry_week_rule":     "별도 플레이북 적용",
    "msci_rebal_alert":     True,
    "broker":               "Kiwoom OpenAPI+",

    # 마찰비용 현실
    "commission_one_way":   0.00015,       # 0.015% 편도
    "slippage_estimate":    0.0002,        # 틱당 추정 슬리피지
    "min_edge_threshold":   0.0005,        # 비용 초과 기대알파
}
```

### 3.3 한국 옵션시장 구조적 특성 (필수 인식)

```
1. 유동성 편중
   ATM 전후 2~3개 행사가에 거래량 90% 집중
   → 깊은 OTM 옵션은 스프레드 위험 극단적

2. 만기일 왜곡 (매월 두 번째 목요일)
   Gamma Pinning 효과 극대화
   → 만기 당일 전략은 별도 룰북 적용

3. 외국인 수급 지배력
   옵션 시장 외국인 비중 60-70%
   → 외국인 방향이 레짐 판단의 핵심 입력

4. 개인투자자 역지표성
   개인 순매수 급증 = 역발상 신호로 처리

5. 단기 감마 쏠림
   만기 1-2주 이내 ATM 옵션에 포지션 집중
   → 단기 Gamma Wall 효과 미국보다 강함
```

---

## 4. SYSTEM ARCHITECTURE — 전체 구조

```
┌─────────────────────────────────────────────────────────────────────────┐
│                       MAHDI v5 COMMAND CENTER                           │
│                                                                         │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │  LAYER 0: DATA INGESTION (Kiwoom API + Global Feed)            │    │
│  │  tick → clean → feature → signal → decision → execution → log  │    │
│  └──────────────────────────┬──────────────────────────────────────┘    │
│                             │                                           │
│  ┌──────────┐  ┌──────────┐ │ ┌──────────┐  ┌──────────┐              │
│  │ ENGINE 1 │  │ ENGINE 2 │ │ │ ENGINE 3 │  │ ENGINE 4 │              │
│  │  REGIME  │  │  ORDER   │ │ │ OPTIONS  │  │  GREEKS  │              │
│  │ HMM+Bayes│  │  FLOW    │ │ │SABR+GEX  │  │   PnL    │              │
│  │          │  │  VPIN    │ │ │ Vanna-   │  │ ATTRIB   │              │
│  │          │  │  LOB     │ │ │  Volga   │  │          │              │
│  └────┬─────┘  └────┬─────┘ │ └────┬─────┘  └────┬─────┘              │
│       │              │       │      │              │                    │
│  ┌────▼──────────────▼───────▼──────▼──────────────▼──────────────┐    │
│  │              ENGINE 5: SIGNAL FUSION + META-LABELING            │    │
│  │        [XGBoost | LSTM | Transformer | HMM Ensemble]           │    │
│  │        Primary Signal → Regime Weight → Meta Filter             │    │
│  └─────────────────────────────┬───────────────────────────────────┘    │
│                                │                                        │
│  ┌─────────────────────────────▼───────────────────────────────────┐    │
│  │              ENGINE 6: STRATEGY PALETTE                         │    │
│  │  Directional | Vol Structure | Relative Value | Tail Hedge      │    │
│  └─────────────────────────────┬───────────────────────────────────┘    │
│                                │                                        │
│  ┌──────────────┐  ┌───────────▼──────────┐  ┌──────────────────┐      │
│  │  ENGINE 7    │  │     ENGINE 8         │  │    ENGINE 9      │      │
│  │  RISK BRAIN  │  │  EXECUTION &         │  │  SELF-LEARNING   │      │
│  │  Kelly+CPPI  │  │  EXIT ORCHESTRATOR   │  │  Online+WFO      │      │
│  │  Portfolio Δ │  │  Almgren-Chriss      │  │  Drift Detection │      │
│  └──────────────┘  └──────────────────────┘  └──────────────────┘      │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 5. ENGINE 1: REGIME INTELLIGENCE ENGINE

> 비유: 날씨예보관. 단순히 "비가 온다"가 아니라 "저기압 시스템의 이동 속도와 강수 확률 분포"를 계산한다.

### 5.1 상태 공간 (8-Regime System)

```python
from enum import IntEnum

class Regime(IntEnum):
    TREND_UP_STRONG    = 0   # 상승추세 강함   → Long Gamma, Momentum
    TREND_DOWN_STRONG  = 1   # 하락추세 강함   → Put Buy, Debit Spread
    RANGE_BALANCED     = 2   # 균형 레인지     → Mean Reversion, Premium Decay
    RANGE_BREAK_PREP   = 3   # 압축 후 팽창대기 → Straddle 준비
    VOL_EXPANSION      = 4   # 변동성 팽창     → Long Gamma 우세
    VOL_COMPRESSION    = 5   # 변동성 압축     → Breakout 대기
    LIQUIDITY_THIN     = 6   # 유동성 빈약     → 규모 축소, 선택적 진입
    CRISIS_DEFENSE     = 7   # 위기 상태       → 신규진입 금지, Tail Hedge

# 레짐별 전략 가중치 스위치
REGIME_STRATEGY_WEIGHTS = {
    Regime.TREND_UP_STRONG:   {"momentum": 1.0, "mean_rev": 0.0, "vol_buy": 0.5, "vol_sell": 0.0},
    Regime.TREND_DOWN_STRONG: {"momentum": 1.0, "mean_rev": 0.0, "vol_buy": 0.5, "vol_sell": 0.0},
    Regime.RANGE_BALANCED:    {"momentum": 0.0, "mean_rev": 1.0, "vol_buy": 0.0, "vol_sell": 0.6},
    Regime.RANGE_BREAK_PREP:  {"momentum": 0.3, "mean_rev": 0.0, "vol_buy": 1.0, "vol_sell": 0.0},
    Regime.VOL_EXPANSION:     {"momentum": 0.5, "mean_rev": 0.0, "vol_buy": 1.0, "vol_sell": 0.0},
    Regime.VOL_COMPRESSION:   {"momentum": 0.0, "mean_rev": 0.5, "vol_buy": 0.8, "vol_sell": 0.0},
    Regime.LIQUIDITY_THIN:    {"momentum": 0.2, "mean_rev": 0.2, "vol_buy": 0.2, "vol_sell": 0.0},
    Regime.CRISIS_DEFENSE:    {"momentum": 0.0, "mean_rev": 0.0, "vol_buy": 0.0, "vol_sell": 0.0},
}
```

### 5.2 레짐 탐지 입력 변수

```python
class RegimeFeatureSet:
    """
    비유: 날씨를 판단할 때 온도, 습도, 기압, 풍속을
    하나씩 보는 게 아니라 종합해서 판단하는 것과 같다.
    """

    # --- 추세 측정 ---
    def hurst_exponent(self, prices: pd.Series, lags=range(2, 50)) -> float:
        """
        R/S Analysis로 시장의 '기억력' 측정
        H > 0.6 → 추세 지속 (모멘텀 전략 유리)
        H < 0.4 → 평균 회귀 (Mean Reversion 유리)
        H ≈ 0.5 → 랜덤워크 (예측 불가)
        """
        tau = [np.std(np.subtract(prices[lag:].values,
                                   prices[:-lag].values)) for lag in lags]
        reg = np.polyfit(np.log(lags), np.log(tau), 1)
        return reg[0]

    def adx(self, high, low, close, period=14) -> float:
        """Wilder ADX: >25 추세 확정, >40 강한 추세"""

    # --- 변동성 측정 ---
    def realized_vol_ratio(self, returns, short=5, long=20) -> float:
        """RV(short) / RV(long): >1.3 → 변동성 팽창"""
        rv_short = returns.rolling(short).std() * np.sqrt(252 * 6.5 * 60)
        rv_long  = returns.rolling(long).std()  * np.sqrt(252 * 6.5 * 60)
        return (rv_short / rv_long).iloc[-1]

    def garman_klass_vol(self, o, h, l, c) -> float:
        """
        Garman-Klass (1980): OHLC 기반 분산 추정
        단순 Close-Close보다 정확도 7배 높음
        비유: 종가만 보는 것 vs OHLC 전체를 보는 것
        """
        return np.sqrt(
            0.5 * (np.log(h/l))**2
            - (2*np.log(2)-1) * (np.log(c/o))**2
        )

    # --- 크로스에셋 ---
    def cross_asset_stress(self) -> dict:
        """
        USDKRW 변화율    → 외국인 자금흐름 선행
        VIX term slope   → 글로벌 공포 구조
        USDCNH           → 중국 리스크온/오프 (KOSPI 상관 0.75+)
        US 10Y-2Y Spread → 경기 선행 (역전=위험)
        Copper/Gold      → 경기 민감도
        """
```

### 5.3 Bayesian Regime Scoring

```python
class BayesianRegimeScorer:
    """
    단일 지표가 아니라 베이지안 점수 합산으로 레짐 확률 산출
    비유: 의사가 하나의 증상만 보는 게 아니라
          여러 검사 결과를 종합해 진단하는 것
    """

    def score(self, features: dict) -> np.ndarray:
        """
        Returns: 8개 레짐에 대한 확률 벡터 (합 = 1.0)

        우선 규칙:
        - 1분 레짐과 15분 레짐 충돌 시 → 상위(15분) 우선
        - CRISIS_DEFENSE 진입 시 → 하위 레짐 무시
        - 장 초반 10분 → 레짐 확률 불안정, 거래 사이즈 50% 감소
        """
        scores = np.zeros(8)

        # Hurst > 0.6 이면 TREND 레짐 점수 가산
        if features["hurst"] > 0.6:
            scores[0] += 2.0  # TREND_UP
            scores[1] += 2.0  # TREND_DOWN (방향은 다른 신호로)

        # ADX > 25 추세 확인
        if features["adx"] > 25:
            scores[0] += 1.5
            scores[1] += 1.5

        # RV ratio > 1.3 변동성 팽창
        if features["rv_ratio"] > 1.3:
            scores[4] += 3.0  # VOL_EXPANSION
            scores[0] -= 1.0  # TREND 점수 감소

        # VIX term backwardation + USDKRW 급등
        if features["vix_backwardation"] and features["usdkrw_spike"]:
            scores[7] += 5.0  # CRISIS_DEFENSE

        return self._softmax(scores)
```

---

## 6. ENGINE 2: ORDER FLOW & MICROSTRUCTURE ENGINE

> 비유: 법의학자. 시체(캔들차트)를 보는 게 아니라, 사망 원인(주문흐름)을 해부한다.

### 6.1 VPIN — 주문 독성 실시간 측정

```python
def calculate_vpin(
    volume: pd.Series,
    buy_vol: pd.Series,
    bucket_size: int = 50
) -> pd.Series:
    """
    Easley, Lopez de Prado, O'Hara (2012)
    "Flow Toxicity and Liquidity in a High-frequency World"
    → 2010 Flash Crash 6시간 전에 VPIN이 급등함을 사후 검증

    VPIN > 0.7 : 정보거래자 활성화 (방향성 추종 또는 방어 강화)
    VPIN 급등  : 시장 불안정성 선행 신호 (MM들이 호가 회수 시작)

    비유: 연못의 물고기가 일제히 한쪽으로 움직이기 시작하면
          무언가 큰 것이 다가오고 있다는 신호
    """
    sell_vol = volume - buy_vol
    results = []
    i = 0
    while i < len(volume):
        b = slice(i, i + bucket_size)
        imb = abs(buy_vol[b].sum() - sell_vol[b].sum())
        tot = volume[b].sum()
        results.append(imb / tot if tot > 0 else 0)
        i += bucket_size
    return pd.Series(results)
```

### 6.2 Avellaneda-Stoikov 마켓메이킹 모델

```python
class AvellanedaStoikovMM:
    """
    Avellaneda & Stoikov (2008)
    "High-frequency trading in a limit order book"

    최적 호가(Reservation Price) 수식:
    r(s, t) = s - q · γ · σ² · (T - t)

    최적 스프레드 수식:
    δ_bid + δ_ask = γ · σ² · (T - t) + (2/γ) · ln(1 + γ/κ)

    비유: 환전소 주인이 환율을 어떻게 제시할지 계산하는 것.
    재고(포지션)가 한쪽으로 쏠리면 반대편을 더 싸게 제시해
    재고를 균형으로 되돌린다.

    MAHDI에서의 적용:
    → 장중 진입 호가 설정 최적화
    → 포지션 방향에 따른 공격성 자동 조절
    """

    def __init__(self, gamma: float = 0.1, sigma: float = 0.02,
                 kappa: float = 1.5, T: float = 6.5 * 3600):
        self.gamma = gamma   # 리스크 회피도
        self.sigma = sigma   # 기초자산 변동성
        self.kappa = kappa   # 주문 도달 강도
        self.T = T           # 세션 총 시간 (초)

    def reservation_price(self, s: float, q: float, t: float) -> float:
        """
        s: 현재 mid price
        q: 현재 재고 포지션 (양수=롱, 음수=숏)
        t: 경과 시간 (초)
        """
        return s - q * self.gamma * self.sigma**2 * (self.T - t)

    def optimal_spread(self, t: float) -> tuple:
        """Returns (bid_offset, ask_offset)"""
        time_factor = self.gamma * self.sigma**2 * (self.T - t)
        base_spread = (2 / self.gamma) * np.log(1 + self.gamma / self.kappa)
        half = (time_factor + base_spread) / 2
        return half, half   # 중립 포지션 기준
```

### 6.3 LOB (Limit Order Book) 이벤트 분석

```python
class LOBEventTagger:
    """
    호가창 이벤트를 실시간으로 분류 · 태깅
    비유: 주식 시장의 CCTV. 누가 어떤 행동을 했는지 기록.
    """

    EVENT_TYPES = {
        "SWEEP_BUY":       "공격적 시장가 매수 (시세 추격)",
        "SWEEP_SELL":      "공격적 시장가 매도 (시세 추격)",
        "ICEBERG_DETECT":  "빙산 주문 감지 (대규모 분할 체결)",
        "QUOTE_STUFF":     "호가 도배 후 취소 (노이즈 주입)",
        "STACK_BUILD_BID": "매수 호가 적층 (지지 형성 의도)",
        "STACK_PULL_BID":  "매수 호가 대량 취소 (지지 철수)",
        "ABSORPTION":      "대량 매도에도 가격 유지 (기관 매수 흡수)",
        "EXHAUSTION":      "대량 거래 후 가격 정체 (추세 소진)",
    }

    def microprice(self, bid: float, ask: float,
                    bid_size: float, ask_size: float) -> float:
        """
        Gatheral (2010): 호가잔량 가중 중심가격
        단순 mid보다 10-15ms 선행하는 가격 추정치

        비유: 줄다리기에서 양쪽의 힘(잔량)에 따라
              줄이 어느 쪽으로 움직일지 예측
        """
        total = bid_size + ask_size
        return (bid * ask_size + ask * bid_size) / total

    def queue_imbalance(self, bid_qty: float, ask_qty: float) -> float:
        """
        Stoikov (2018): 호가 불균형 지수
        > 0.3  → 매수 압력 우세
        < -0.3 → 매도 압력 우세
        """
        return (bid_qty - ask_qty) / (bid_qty + ask_qty)
```

### 6.4 주체별 포지션 트래킹 (한국 특화)

```python
class KoreanInstitutionalTracker:
    """
    한국 옵션시장 수급 3주체 분석
    외국인 = 정보 보유 큰손 (선행 지표)
    기관   = 분기 실적 종속 큰손 (후행 경향)
    개인   = 감정 거래자 (역지표로 활용)
    """

    def entity_vwap(self, trades: pd.DataFrame, entity: str) -> float:
        """
        각 주체의 당일 누적 평균 매입단가
        현재가 vs VWAP 차이 → 수익/손실 상태 추적
        """
        filtered = trades[trades["entity"] == entity]
        return (filtered["price"] * filtered["volume"]).sum() / filtered["volume"].sum()

    def smart_money_alignment(self,
                               foreign_net: float,
                               institution_net: float,
                               retail_net: float) -> dict:
        """
        수급 정렬 점수 계산
        외국인 + 기관 같은 방향 = 강한 추세 신호
        외국인 vs 개인 역방향  = Trap 가능성 높음
        """
        smart = foreign_net + institution_net
        dumb  = retail_net

        alignment = {
            "direction":   1 if smart > 0 else -1,
            "trap_signal": abs(smart) > 0 and np.sign(smart) != np.sign(dumb),
            "conviction":  min(abs(smart) / (abs(smart) + abs(dumb) + 1e-8), 1.0),
        }
        return alignment
```

---

## 7. ENGINE 3: OPTIONS INTELLIGENCE ENGINE

> 비유: 기상위성. 주식시장이 날씨라면, 옵션시장은 기압 위성사진이다. 폭풍이 오기 전에 이미 구조가 보인다.

### 7.1 SABR 모델 — Skew 정밀 캘리브레이션

```python
class SABRModel:
    """
    Hagan, Kumar, Lesniewski, Woodward (2002)
    "Managing Smile Risk"

    dF = σ · F^β · dW₁
    dσ = α · σ · dW₂
    corr(dW₁, dW₂) = ρ

    파라미터:
    α (alpha): 초기 변동성 레벨
    β (beta) : 탄력성 (0=Normal, 0.5=CIR, 1=Lognormal)
    ρ (rho)  : 가격-변동성 상관관계 (레버리지 효과)
    ν (nu)   : 변동성의 변동성 (vol-of-vol)

    비유: Heston이 날씨 전체의 기후 모델이라면,
          SABR은 오늘 하루 날씨의 정밀 예보.
          특히 극단적 행사가(OTM)의 스마일 형태를 잘 잡는다.

    MAHDI 적용:
    → IV Surface 보간 (행사가 사이 빈칸 채우기)
    → 25-delta / 10-delta Skew 정밀 추출
    → Skew 왜곡 이상치 탐지 → 기회 신호
    """

    def implied_vol_sabr(self, F: float, K: float, T: float,
                          alpha: float, beta: float,
                          rho: float, nu: float) -> float:
        """SABR 근사식 (Hagan 2002 공식)"""
        if abs(F - K) < 1e-8:   # ATM 근사
            FK_mid = F
            atm_vol = (alpha / (FK_mid**(1-beta))) * (
                1 + ((1-beta)**2 / 24 * alpha**2 / FK_mid**(2*(1-beta))
                     + rho * beta * nu * alpha / (4 * FK_mid**(1-beta))
                     + (2 - 3*rho**2) / 24 * nu**2) * T
            )
            return atm_vol
        else:
            z = (nu / alpha) * (F*K)**((1-beta)/2) * np.log(F/K)
            x = np.log((np.sqrt(1 - 2*rho*z + z**2) + z - rho) / (1 - rho))

            num = alpha
            denom1 = (F*K)**((1-beta)/2) * (
                1 + (1-beta)**2/24 * np.log(F/K)**2
                  + (1-beta)**4/1920 * np.log(F/K)**4
            )
            denom2 = z / (x + 1e-12)

            correction = (
                1 + ((1-beta)**2 / 24 * alpha**2 / (F*K)**(1-beta)
                     + rho*beta*nu*alpha / (4*(F*K)**((1-beta)/2))
                     + (2 - 3*rho**2) / 24 * nu**2) * T
            )
            return (num / denom1) * denom2 * correction

    def calibrate(self, strikes: np.ndarray, market_ivs: np.ndarray,
                   F: float, T: float) -> dict:
        """시장 IV에 SABR 파라미터 피팅 (scipy optimize)"""
        from scipy.optimize import minimize

        def objective(params):
            alpha, rho, nu = params
            beta = 0.5   # 한국 지수 옵션: β=0.5 (lognormal-normal 중간)
            model_ivs = np.array([
                self.implied_vol_sabr(F, K, T, alpha, beta, rho, nu)
                for K in strikes
            ])
            return np.sum((model_ivs - market_ivs)**2)

        result = minimize(objective, x0=[0.2, -0.3, 0.3],
                          bounds=[(0.01, 2.0), (-0.99, 0.99), (0.01, 2.0)])
        alpha, rho, nu = result.x
        return {"alpha": alpha, "beta": 0.5, "rho": rho, "nu": nu}
```

### 7.2 Vanna-Volga Pricing — 스마일 보정

```python
class VannaVolgaPricer:
    """
    Castagna & Mercurio (2007)
    "Vanna-Volga methods applied to FX derivatives"

    FX 시장에서 검증된 방법을 지수 옵션에 적용
    세 개의 기준 옵션(25-delta put, ATM, 25-delta call)으로
    임의 행사가의 스마일 가격 조정

    조정된 가격 = BS가격 + Vanna·(RR비용) + Volga·(BF비용)

    비유: 기본 지도(BS)에 지형 보정(Vanna)과
          고도 보정(Volga)을 추가하는 것

    MAHDI 적용:
    → 유동성 낮은 행사가의 공정 IV 추정
    → 과소/과대 평가된 행사가 탐지
    → 상대가치 기회 식별
    """

    def price_with_smile(self,
                          S, K, T, r,
                          sigma_atm: float,   # ATM IV
                          rr_25d: float,      # 25-delta Risk Reversal
                          bf_25d: float,      # 25-delta Butterfly
                          option_type: str) -> dict:
        """
        Returns: {bs_price, smile_adjustment, vv_price, vanna, volga}
        """
        # 기준 행사가 계산 (25-delta)
        from scipy.stats import norm
        d2_atm = (np.log(S/S) + (r - 0.5*sigma_atm**2)*T) / (sigma_atm*np.sqrt(T))

        # BS 기본 가격
        bs = self._bs_price(S, K, T, r, sigma_atm, option_type)

        # Vanna: dDelta/dVol → Risk Reversal로 헷지
        vanna = self._vanna(S, K, T, r, sigma_atm)

        # Volga: dVega/dVol → Butterfly로 헷지
        volga = self._volga(S, K, T, r, sigma_atm)

        # 스마일 조정 비용
        rr_cost = rr_25d  # 25d call IV - 25d put IV
        bf_cost = bf_25d  # (25d call IV + 25d put IV)/2 - ATM IV

        smile_adj = vanna * rr_cost + volga * bf_cost

        return {
            "bs_price":       bs,
            "smile_adj":      smile_adj,
            "vv_price":       bs + smile_adj,
            "vanna":          vanna,
            "volga":          volga,
        }
```

### 7.3 Gamma Exposure (GEX) — 딜러 헷지 지형

```python
class GammaExposureEngine:
    """
    딜러의 감마 포지션이 시장을 어떻게 움직이는지 계산
    비유: 시장에 보이지 않는 자석이 있다.
          GEX > 0 → 자석이 가격을 당기는 힘이 있다 (안정화)
          GEX < 0 → 자석이 가격을 밀어내는 힘이 있다 (불안정화)
    """

    def total_gex(self, options_chain: pd.DataFrame,
                   spot: float, multiplier: int = 250_000) -> float:
        """
        GEX = Σ (Gamma × OI × Multiplier × Spot²) for Calls
            - Σ (Gamma × OI × Multiplier × Spot²) for Puts

        KOSPI200 옵션 승수: 250,000원 (미니: 50,000원)
        """
        calls = options_chain[options_chain["type"] == "C"]
        puts  = options_chain[options_chain["type"] == "P"]

        gex_call = (calls["gamma"] * calls["oi"] * multiplier * spot**2 / 100).sum()
        gex_put  = (puts["gamma"]  * puts["oi"]  * multiplier * spot**2 / 100).sum()

        return gex_call - gex_put

    def gamma_flip_level(self, options_chain: pd.DataFrame,
                          spot: float, scan_range: float = 0.05) -> float:
        """
        Gamma Flip: GEX가 양→음으로 전환되는 가격대
        이 레벨 이탈 시 변동성 폭발 가능성 급증

        비유: 댐의 수위 임계점.
              이 레벨 아래로 떨어지면 물이 쏟아진다.
        """
        strikes = np.linspace(spot * (1 - scan_range),
                               spot * (1 + scan_range), 100)
        gex_by_level = []
        for s in strikes:
            gex_by_level.append((s, self.total_gex(options_chain, s)))

        # 부호 전환점 탐색
        for i in range(1, len(gex_by_level)):
            if gex_by_level[i-1][1] * gex_by_level[i][1] < 0:
                return (gex_by_level[i-1][0] + gex_by_level[i][0]) / 2
        return None

    def charm_flow(self, options_chain: pd.DataFrame,
                    spot: float, multiplier: int = 250_000) -> float:
        """
        Charm (dDelta/dt): 시간 경과에 따른 Delta 변화
        딜러의 일중 리밸런싱 방향 예측

        장 마감 1-2시간 전: Charm 방향으로 가격 드리프트 발생
        비유: 조류가 바뀌기 전에 조류의 방향이 약해지는 것
        """
        calls = options_chain[options_chain["type"] == "C"]
        puts  = options_chain[options_chain["type"] == "P"]

        charm_call = (calls["charm"] * calls["oi"] * multiplier).sum()
        charm_put  = (puts["charm"]  * puts["oi"]  * multiplier).sum()

        return charm_call - charm_put
```

### 7.4 Variance Swap & VRP 포착

```python
class VarianceSwapEngine:
    """
    Carr & Madan (1998): 옵션 포트폴리오로 분산스왑 복제
    Carr & Wu (2009): Variance Risk Premium 측정

    분산스왑의 공정 분산 (Model-Free):
    Kvar = (2/T) · Σ [ΔK/K²] · e^(rT) · Option_Price(K)

    VRP = Implied Variance - Realized Variance

    VRP > 0 (대부분의 시간): 옵션 매도자가 장기적으로 유리
    VRP < 0 (위기 직전):     옵션 매수자 유리 (보험 가치 상승)

    비유: 화재보험 시장.
    VRP > 0 = 보험회사가 평균적으로 이익
    VRP < 0 = 화재 위험이 실제로 더 높은 상황
    """

    def model_free_implied_var(self,
                                calls: pd.DataFrame,
                                puts: pd.DataFrame,
                                F: float,
                                T: float,
                                r: float = 0.035) -> float:
        """
        Carr-Madan 모델 독립적 분산 계산
        행사가 전체를 적분해 옵션 가격으로 미래 분산 추출
        """
        # OTM 옵션만 사용 (K < F: puts, K > F: calls)
        otm_puts  = puts[puts["strike"] < F].copy()
        otm_calls = calls[calls["strike"] > F].copy()

        def integrate_options(options: pd.DataFrame, is_put: bool) -> float:
            options = options.sort_values("strike")
            dk = np.diff(options["strike"].values)
            k  = options["strike"].values[:-1]
            p  = options["price"].values[:-1]
            return 2 * np.sum((dk / k**2) * np.exp(r * T) * p) / T

        var_put  = integrate_options(otm_puts,  is_put=True)
        var_call = integrate_options(otm_calls, is_put=False)

        return var_put + var_call

    def vrp_signal(self, implied_var: float, realized_var: float,
                    lookback: int = 20) -> dict:
        """
        VRP 트레이딩 신호
        VRP_z > 1.5  → 변동성 매도 기회 (IV 과대)
        VRP_z < -1.5 → 변동성 매수 기회 (IV 과소, 위기 경계)
        """
        vrp = implied_var - realized_var
        return {
            "vrp":       vrp,
            "direction": "sell_vol" if vrp > 0 else "buy_vol",
            "magnitude": abs(vrp),
        }
```

### 7.5 Dispersion Trading (분산 트레이딩)

```python
class DispersionTrading:
    """
    헤지펀드 핵심 전략: 지수 IV vs 구성종목 IV 불일치 포착

    이론: Index IV ≤ √(Σ wᵢ² · σᵢ²) + Σᵢ≠ⱼ wᵢwⱼσᵢσⱼρᵢⱼ

    실전 관찰: 지수 IV는 보통 구성종목 IV보다 높다 (상관관계 프리미엄)
    → 지수 IV 매도 + 구성종목 IV 매수 = 상관관계 프리미엄 수집

    KOSPI200 적용:
    - KOSPI200 ATM IV 매도 (Variance Swap or Straddle Sell)
    - 삼성전자, SK하이닉스 등 상위 구성 종목 IV 매수
    - 이익 = 실현 상관관계 하락 시

    비유: 오케스트라 전체 소음이 각 악기 소음보다 클 때
          그 차이를 수익화하는 것
    """

    def implied_correlation(self,
                             index_var: float,
                             stock_vars: list,
                             weights: list) -> float:
        """
        내재 상관관계 = 시장이 가격에 반영한 종목 간 평균 상관관계
        이 값이 역사적 실현 상관관계보다 크면 지수 IV가 상대적으로 비쌈
        """
        weighted_var_sum = sum(w**2 * v for w, v in zip(weights, stock_vars))
        cross_term_denom = (sum(w * np.sqrt(v) for w, v in zip(weights, stock_vars)))**2

        if cross_term_denom <= weighted_var_sum:
            return 0.0
        return (index_var - weighted_var_sum) / (cross_term_denom - weighted_var_sum)
```

### 7.6 Put-Call Parity Deviation Monitor

```python
def pcparity_deviation(
    call_price: float, put_price: float,
    S: float, K: float, T: float, r: float
) -> float:
    """
    Put-Call Parity: C - P = S - K·e^(-rT)

    이탈 시 = 차익거래 기회 또는 수급 왜곡 신호

    비유: 저울의 양쪽이 맞지 않으면
          누군가 한쪽에 몰래 무게를 올려놓은 것
    """
    theoretical = S - K * np.exp(-r * T)
    actual = call_price - put_price
    return actual - theoretical   # 양수: Call 비쌈, 음수: Put 비쌈
```

---

## 8. ENGINE 4: GREEKS PnL ATTRIBUTION ENGINE

> 비유: 재무 대차대조표. 기업이 어디서 돈을 벌고 잃는지처럼, 포지션이 어느 Greeks에서 손익이 발생했는지 실시간으로 회계 처리한다.

### 8.1 포트폴리오 Greeks 집계

```python
class PortfolioGreeksEngine:
    """
    개별 포지션의 Greeks를 포트폴리오 단위로 집계
    "Greek 관리 = 포지션 관리"

    Delta  : 기초자산 1포인트 변동 시 손익
    Gamma  : Delta의 변화율 (볼록성)
    Theta  : 하루 시간가치 감소
    Vega   : IV 1% 변동 시 손익
    Rho    : 금리 1bp 변동 시 손익
    Vanna  : dDelta/dVol = Delta-Vega 교차 리스크
    Volga  : dVega/dVol = Vega의 변화율
    Charm  : dDelta/dt  = 시간에 따른 Delta 변화
    """

    def aggregate(self, positions: list) -> dict:
        """포트폴리오 전체 Greeks 합산"""
        total = {"delta": 0, "gamma": 0, "theta": 0,
                 "vega": 0, "rho": 0, "vanna": 0, "volga": 0, "charm": 0}

        for pos in positions:
            sign = 1 if pos["side"] == "LONG" else -1
            qty  = pos["quantity"] * sign
            for greek in total:
                total[greek] += pos[greek] * qty * pos["multiplier"]

        return total

    def pnl_attribution(self,
                         greeks_before: dict,
                         dS: float,           # 가격 변화
                         dV: float,           # IV 변화
                         dt: float,           # 시간 경과 (연율화)
                         dR: float = 0.0      # 금리 변화
                         ) -> dict:
        """
        테일러 전개로 PnL을 Greeks별로 분해
        PnL ≈ δ·ΔS + ½γ·ΔS² + θ·Δt + ν·ΔV + ρ·ΔR
              + Vanna·ΔS·ΔV + ½Volga·ΔV²

        비유: 자동차 연비를 속도, 에어컨, 경사도, 타이어 압력으로
              각각 분해해서 어디서 연료가 소모되는지 확인하는 것
        """
        g = greeks_before
        return {
            "delta_pnl":  g["delta"] * dS,
            "gamma_pnl":  0.5 * g["gamma"] * dS**2,
            "theta_pnl":  g["theta"] * dt,
            "vega_pnl":   g["vega"] * dV,
            "rho_pnl":    g["rho"] * dR,
            "vanna_pnl":  g["vanna"] * dS * dV,
            "volga_pnl":  0.5 * g["volga"] * dV**2,
            "total_pnl":  sum([
                g["delta"] * dS,
                0.5 * g["gamma"] * dS**2,
                g["theta"] * dt,
                g["vega"] * dV,
                g["vanna"] * dS * dV,
                0.5 * g["volga"] * dV**2,
            ])
        }
```

### 8.2 실시간 Greeks 리스크 대차대조표

```
┌──────────────────────────────────────────────────────────────────┐
│              MAHDI GREEKS RISK BALANCE SHEET                     │
│                              2025-01-15  14:23:05               │
├────────────────┬──────────────┬──────────────┬──────────────────┤
│  GREEK         │  CURRENT     │  LIMIT       │  STATUS          │
├────────────────┼──────────────┼──────────────┼──────────────────┤
│  Portfolio Δ   │  +12,500원   │  ±50,000원   │  🟢 SAFE         │
│  Portfolio Γ   │  +180,000원  │  ±500,000원  │  🟢 SAFE         │
│  Portfolio Θ   │  -45,000원/일│  -100,000원  │  🟡 WATCH        │
│  Portfolio ν   │  +320,000원  │  ±800,000원  │  🟢 SAFE         │
│  Vanna         │  -8,500원    │  ±30,000원   │  🟢 SAFE         │
│  Volga         │  +12,000원   │  ±50,000원   │  🟢 SAFE         │
├────────────────┼──────────────┼──────────────┼──────────────────┤
│  PnL TODAY     │  +₩1,250,000 │              │  +2.1% vs NAV    │
│  Greeks P&L    │  Δ+820K  Γ+680K  Θ-250K     │  ν+0            │
│  Daily VaR 95% │  ₩-380,000   │  ₩-500,000   │  🟢 SAFE         │
├────────────────┴──────────────┴──────────────┴──────────────────┤
│  EXPIRY RISK MAP                                                 │
│  이번주 만기: Δ+8,200  Γ+45,000  Θ-32,000  ν+180,000           │
│  다음주 만기: Δ+4,300  Γ+135,000 Θ-13,000  ν+140,000           │
└──────────────────────────────────────────────────────────────────┘
```

---

## 9. ENGINE 5: SIGNAL FUSION & META-LABELING ENGINE

> 비유: 오케스트라 지휘자. 각 악기(엔진)의 소리를 받아 불협화음을 걸러내고 하모니(최종 신호)만 통과시킨다.

### 9.1 Feature Engineering (Lopez de Prado 방법론)

```python
class FinancialFeatureEngine:
    """
    Advances in Financial Machine Learning (2018) 핵심 기법
    """

    def fractional_differentiation(self,
                                    series: pd.Series,
                                    d: float = 0.4) -> pd.Series:
        """
        d = 1: 완전 차분 (정상성 O, 장기 기억 X)
        d = 0.4: 최적 (정상성 유지 + 장기 기억 보존)

        비유: 빨래를 너무 세게 짜면 형태가 망가지고,
              너무 약하게 짜면 물이 남는다.
              최적 강도(d≈0.4)를 찾는 것.
        """
        weights = self._get_weights_ffd(d, size=len(series))
        result  = np.convolve(series.values, weights[::-1], mode="valid")
        index   = series.index[len(weights)-1:]
        return pd.Series(result, index=index)

    def triple_barrier_labeling(self,
                                 df: pd.DataFrame,
                                 pt_sl: list,       # [profit_take, stop_loss]
                                 min_ret: float,
                                 num_threads: int = 4) -> pd.Series:
        """
        3중 배리어 레이블링:
        +1 = 상단 배리어 (익절) 먼저 도달
        -1 = 하단 배리어 (손절) 먼저 도달
         0 = 시간 배리어 (기간 만료)

        비유: 경마에서 세 개의 결승선을 그어놓고
              말이 어느 선에 먼저 닿는지 기록하는 것
        """

    def sample_weights_time_decay(self,
                                   df: pd.DataFrame,
                                   decay: float = 1.0) -> pd.Series:
        """
        최신 샘플에 더 높은 가중치 (decay = 1: 선형, < 1: 볼록)
        비유: 최근 날씨일수록 오늘 날씨 예측에 더 중요
        """
```

### 9.2 Combinatorial Purged Cross-Validation

```python
class CombinatorialPurgedCV:
    """
    Lopez de Prado (2018) + Bailey & Lopez de Prado (2014)

    일반 K-Fold의 두 가지 치명적 문제:
    1. Leakage: 훈련셋에 테스트 미래 정보가 포함
    2. Overlap: 레이블이 겹치는 샘플이 분리 안 됨

    Combinatorial Purged CV:
    → 여러 테스트셋 조합 → 더 많은 백테스트 경로 생성
    → Embargo: 테스트셋 앞뒤로 데이터 제거 (누수 차단)

    비유: 시험 문제를 만들 때 교재를 보여준 후
          다른 교재로 시험을 치는 것이 아니라,
          완전히 다른 시기의 문제로 시험 치는 것
    """

    def split(self,
               X: pd.DataFrame,
               y: pd.Series,
               embargo_pct: float = 0.01) -> list:
        """
        Returns: list of (train_idx, test_idx) tuples
        각 fold에서 embargo 구간 제거됨
        """
```

### 9.3 Meta-Labeling (2단계 모델)

```python
class MetaLabelingSystem:
    """
    Lopez de Prado (2018) 핵심 혁신
    비유:
    1차 모델 = 탐정 ("이 사람이 범인인 것 같다")
    2차 모델 = 검사 ("탐정의 판단을 증거로 기소할 수 있는가")

    1차: 방향 예측 (Long/Short/Flat) — 재현율(Recall) 극대화
    2차: 1차 신호를 믿을지 결정    — 정밀도(Precision) 극대화
    최종: 포지션 크기 = 방향 × 확률
    """

    def train_primary(self, features: pd.DataFrame,
                       labels: pd.Series) -> None:
        """
        1차 모델: 방향성 예측
        XGBoost + HMM Ensemble
        목표: 높은 Recall (좋은 기회를 놓치지 않는다)
        """
        from xgboost import XGBClassifier
        self.primary = XGBClassifier(n_estimators=200, max_depth=6)
        self.primary.fit(features, labels)

    def train_meta(self, features: pd.DataFrame,
                    primary_predictions: pd.Series,
                    true_labels: pd.Series) -> None:
        """
        2차 모델: 1차 신호 필터링
        입력: [원래 피처 + 1차 모델 예측값]
        출력: 이 신호를 실행할 확률 (0~1)
        목표: 높은 Precision (실행할 때만 맞힌다)
        """
        from sklearn.ensemble import RandomForestClassifier
        meta_features = pd.concat([features, primary_predictions], axis=1)
        meta_labels   = (primary_predictions == true_labels).astype(int)

        self.meta = RandomForestClassifier(n_estimators=100)
        self.meta.fit(meta_features, meta_labels)

    def get_conviction(self, features: pd.DataFrame) -> dict:
        """
        최종 출력:
        direction: -1, 0, 1
        confidence: 0.0 ~ 1.0
        bet_size: Kelly 조정 사이즈

        배팅 크기 = 방향 × 메타 확률
        """
        direction   = self.primary.predict(features)[0]
        meta_prob   = self.meta.predict_proba(
            pd.concat([features,
                       pd.Series([direction], name="primary")], axis=1)
        )[0][1]

        return {
            "direction":  direction,
            "confidence": meta_prob,
            "trade_size": self._bet_size(meta_prob),
        }

    def _bet_size(self, prob: float) -> str:
        """확률 기반 포지션 크기 결정"""
        if prob < 0.55:   return "NO_TRADE"
        elif prob < 0.65: return "SMALL"
        elif prob < 0.75: return "STANDARD"
        else:             return "HIGH_CONVICTION"
```

### 9.4 Deflated Sharpe Ratio (과최적화 탐지)

```python
def deflated_sharpe_ratio(
    sharpe: float,
    n_trials: int,       # 파라미터 탐색 횟수
    obs: int,            # 관측치 수
    skewness: float = 0.0,
    kurtosis: float = 3.0
) -> float:
    """
    Bailey & Lopez de Prado (2014)
    "The Deflated Sharpe Ratio"

    수백 번 파라미터 탐색 후 높은 Sharpe가 나왔다면?
    → 그 중 최선을 고른 것 = 행운 가능성 높음

    DSR: 다중 탐색 보정 후 통계적으로 의미 있는 Sharpe
    실전 기준: DSR > 1.0 이어야 실전 배치 검토

    비유: 주사위를 100번 던져 6이 나왔을 때
          "6이 잘 나오는 주사위"라고 결론 내리는 오류를 방지
    """
    from scipy.stats import norm

    # 최적 기대 최대 Sharpe (다중 탐색 보정)
    e_max_sr = (
        (1 - np.euler_gamma) * norm.ppf(1 - 1/n_trials) +
        np.euler_gamma * norm.ppf(1 - 1/(n_trials * np.e))
    )

    # 비정규성 보정
    sr_adj = sharpe * np.sqrt((1 - skewness*sharpe
                               + (kurtosis-1)/4 * sharpe**2) / obs)

    return norm.cdf((sr_adj - e_max_sr) * np.sqrt(obs - 1))
```

---

## 10. ENGINE 6: STRATEGY PALETTE ENGINE

### 10.1 전략 선택 매트릭스 (레짐 × IV 환경)

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    OPTIONS STRATEGY SELECTION MATRIX                    │
│                                                                         │
│  레짐 \ IV환경    │ IV 낮음 (IVR<30) │ IV 적정 (30-70)  │ IV 높음 (>70) │
│ ────────────────┼──────────────────┼──────────────────┼──────────────── │
│ TREND_UP_STR    │ ATM Call Buy     │ Call Debit Spread│ Ratio Call Spr  │
│                 │ 더 공격적으로    │ 비용 절감        │ IV 부담 경감    │
│ ────────────────┼──────────────────┼──────────────────┼──────────────── │
│ TREND_DN_STR    │ ATM Put Buy      │ Put Debit Spread │ Ratio Put Spr   │
│                 │ 빠른 수익화      │ 비용 절감        │ IV 부담 경감    │
│ ────────────────┼──────────────────┼──────────────────┼──────────────── │
│ RANGE_BALANCED  │ OTM Strangle Buy │ Iron Condor      │ Short Strangle  │
│                 │ 이탈 기대        │ 균형 전략        │ 프리미엄 수집   │
│ ────────────────┼──────────────────┼──────────────────┼──────────────── │
│ RANGE_BRK_PREP  │ ATM Straddle Buy │ Strangle Buy     │ Straddle Buy    │
│                 │ 폭발 준비        │ 비용 절감판      │ IV 불리해도 진입│
│ ────────────────┼──────────────────┼──────────────────┼──────────────── │
│ VOL_EXPANSION   │ ATM Straddle     │ Directional Γ    │ Hedge Only      │
│                 │ Long Gamma       │ 방향성+감마      │ 신규 방향금지   │
│ ────────────────┼──────────────────┼──────────────────┼──────────────── │
│ VOL_COMPRESSION │ Straddle Buy     │ Wait + Prepare   │ Sell Vol Struct │
│                 │ 선제 진입        │ 브레이크아웃대기 │ 제한적 매도     │
└─────────────────────────────────────────────────────────────────────────┘
```

### 10.2 상대가치 전략 — Intraday 적용

```python
class RelativeValueEngine:
    """
    헤지펀드 핵심 전략: 방향성 예측 없이도 구조적 불일치 포착
    비유: 같은 물건이 A마트에서 1000원, B마트에서 1200원이면
          A에서 사서 B에 파는 것
    """

    def call_put_parity_arb(self, calls, puts, F, T, r) -> list:
        """Put-Call Parity 이탈 행사가 탐지"""
        opportunities = []
        for K in calls["strike"].unique():
            c = calls[calls["strike"] == K]["price"].iloc[0]
            p = puts[puts["strike"] == K]["price"].iloc[0]
            deviation = pcparity_deviation(c, p, F, K, T, r)
            if abs(deviation) > 0.5:  # 0.5포인트 이상 이탈
                opportunities.append({
                    "strike": K,
                    "deviation": deviation,
                    "trade": "sell_call_buy_put" if deviation > 0 else "sell_put_buy_call"
                })
        return opportunities

    def cross_expiry_iv_arbitrage(self,
                                   near_iv: float,
                                   far_iv: float,
                                   near_T: float,
                                   far_T: float) -> dict:
        """
        Calendar Spread: 근월 IV 과열 vs 원월 IV 저평가
        비유: 이번 달 보험료가 폭등했는데
              다음 달 보험료는 그대로면 → 스왑 기회
        """
        term_slope = (far_iv - near_iv) / (far_T - near_T)
        fair_near  = far_iv - term_slope * (far_T - near_T)
        iv_gap     = near_iv - fair_near

        return {
            "iv_gap":   iv_gap,
            "signal":  "buy_calendar" if iv_gap > 0.02 else None,
        }
```

### 10.3 Gamma Scalping — 장중 변동성 수익화

```python
class GammaScalpingEngine:
    """
    Long Gamma 포지션 (Straddle/Strangle)에서
    Delta를 주기적으로 헷지해 변동성 수익 실현

    P&L ≈ ½ · Γ · (ΔS)² - Θ · Δt
    → 가격 움직임이 클수록 이익, 시간이 지날수록 손실

    비유: 용수철. 늘어났다 줄었다 할수록 에너지를 얻고,
          가만히 있으면 녹슬어 힘을 잃는다.

    손익분기 조건:
    (ΔS)² > (2 · Θ · Δt) / Γ
    즉, 충분히 움직여야 Theta 비용을 커버
    """

    def delta_hedge_trigger(self,
                             current_delta: float,
                             threshold: float = 0.05) -> bool:
        """
        Delta가 임계치를 초과하면 재헷지
        threshold = 0.05 → 델타가 ±5% 이상 벗어나면 조정
        """
        return abs(current_delta) > threshold

    def pnl_breakeven_move(self,
                            gamma: float,
                            theta: float,
                            dt: float) -> float:
        """
        손익분기 일중 이동폭 (포인트 단위)
        이 이상 움직이면 감마 수익 > 세타 비용
        """
        return np.sqrt(2 * abs(theta) * dt / gamma)
```

---

## 11. ENGINE 7: RISK BRAIN ENGINE

> 비유: 항공기 안전시스템. 자동조종(전략)과 독립적으로, 파일럿이 실수해도 자동으로 안전을 유지한다.

### 11.1 Kelly Criterion + CPPI 자본 보호

```python
class CapitalAllocationEngine:
    """
    공격: Quarter Kelly로 최적 베팅
    방어: CPPI로 자본 하한선 보호

    비유: 카지노에서 이길 때는 Kelly로 베팅을 늘리고,
          일정 금액 이하로 떨어지면 안전금고에 넣는 것
    """

    def quarter_kelly(self,
                       win_prob: float,
                       win_loss_ratio: float) -> float:
        """
        Full Kelly = (p·b - q) / b
        Quarter Kelly = Full Kelly × 0.25

        Full Kelly 대신 1/4만 쓰는 이유:
        - Full Kelly는 이론적으로 완벽하지만
        - 실전에서 파라미터 추정 오차가 크면 파산 위험
        - 비유: 100% 실력 발휘 vs 75% 실력으로 안전하게

        실전 상한: 포트폴리오의 5% 초과 금지
        """
        p = win_prob
        q = 1 - p
        b = win_loss_ratio
        full_kelly = max(0, (p * b - q) / b)
        return min(full_kelly * 0.25, 0.05)

    def cppi_floor(self,
                    nav: float,
                    floor_pct: float = 0.90,
                    multiplier: float = 3.0) -> dict:
        """
        CPPI (Constant Proportion Portfolio Insurance)
        Perold & Sharpe (1988)

        Floor = NAV × 90% (절대 지켜야 할 자본 하한)
        Cushion = NAV - Floor (운용 가능한 여유분)
        Risky Asset = Multiplier × Cushion

        비유: 줄타기할 때 안전망 높이를 정해두는 것.
              안전망까지의 거리(쿠션)에 비례해 위험을 감수.
        """
        floor   = nav * floor_pct
        cushion = max(0, nav - floor)
        risky   = min(multiplier * cushion, nav)

        return {
            "floor":         floor,
            "cushion":       cushion,
            "risky_budget":  risky,
            "safe_budget":   nav - risky,
        }

    def volatility_targeting(self,
                              target_vol: float = 0.15,
                              realized_vol: float = None,
                              nav: float = 1.0) -> float:
        """
        AHL / Man AHL 방식: 변동성 목표치 유지
        비유: 자동차 크루즈 컨트롤.
              길이 험하면 속도 줄이고, 평탄하면 속도 낸다.

        Scaling = target_vol / realized_vol
        → 변동성 낮을 때 포지션 확대
        → 변동성 높을 때 포지션 축소
        """
        if realized_vol is None or realized_vol == 0:
            return 1.0
        return min(target_vol / realized_vol, 2.0)  # 최대 2배 레버리지
```

### 11.2 Dynamic Risk Budget

```python
class DynamicRiskBudget:
    """
    AQR / Bridgewater 방식: 수익이 아닌 '리스크'를 예산으로 배분
    비유: 돈을 나누는 게 아니라 '위험 허용치'를 나누는 것
    """

    LIMITS = {
        "daily_loss_hard":     -0.03,    # -3% 하드 스톱
        "daily_loss_soft":     -0.015,   # -1.5% 소프트 경고
        "single_trade_loss":   -0.005,   # 단일 거래 -0.5%
        "max_drawdown_halt":   -0.10,    # -10% 자동 거래 중단
        "portfolio_delta_abs":  50000,   # 포트폴리오 절대 Delta
        "portfolio_vega_abs":   800000,  # 포트폴리오 절대 Vega
        "max_concurrent_pos":   3,       # 동시 포지션 최대 수
        "max_correlation_pos":  2,       # 동일 방향 최대 수
        "vpin_no_trade":        0.80,    # VPIN 초과 시 신규 진입 금지
    }

    def position_size(self,
                       kelly_fraction: float,
                       regime_confidence: float,
                       meta_confidence: float,
                       liquidity_score: float,
                       drawdown_factor: float,
                       nav: float) -> float:
        """
        최종 포지션 크기 계산:
        Size = NAV × Kelly × Regime × Meta × Liquidity × DrawdownAdj

        각 인수가 곱해지므로 하나라도 낮으면 전체가 줄어든다.
        비유: 체인의 가장 약한 고리가 전체 강도를 결정
        """
        raw_size = (nav
                    * kelly_fraction
                    * regime_confidence
                    * meta_confidence
                    * liquidity_score
                    * drawdown_factor)

        # 상한 적용
        max_size = nav * 0.05  # NAV 5% 상한
        return min(raw_size, max_size)
```

### 11.3 Circuit Breaker & Kill Switch

```python
class CircuitBreaker:
    """자동 거래 차단 시스템. 과부하 시 자동 전원 차단."""

    HALT_CONDITIONS = {
        "daily_loss_pct":     -0.03,
        "weekly_loss_pct":    -0.05,
        "max_drawdown_pct":   -0.10,
        "vpin_extreme":        0.90,
        "vix_spike":           40,
        "usdkrw_move_1d":      0.02,
        "model_drift":         True,
        "data_feed_lag_sec":   5,
        "correlation_break":   True,
    }

    def check(self, market_state: dict, portfolio_state: dict) -> dict:
        """
        Returns: {"halt": bool, "reason": str, "action": str}
        action: "FULL_FLAT" | "REDUCE_50" | "NO_NEW_ENTRY"
        """
        for condition, threshold in self.HALT_CONDITIONS.items():
            value = {**market_state, **portfolio_state}.get(condition)
            if value is not None:
                if isinstance(threshold, bool) and value == threshold:
                    return {"halt": True, "reason": condition,
                            "action": "FULL_FLAT"}
                elif isinstance(threshold, (int, float)) and threshold < 0:
                    if value <= threshold:
                        return {"halt": True, "reason": condition,
                                "action": "FULL_FLAT"}
                elif isinstance(threshold, (int, float)) and threshold > 0:
                    if value >= threshold:
                        return {"halt": True, "reason": condition,
                                "action": "FULL_FLAT"}
        return {"halt": False, "reason": None, "action": None}
```

---

## 12. ENGINE 8: EXECUTION & EXIT ORCHESTRATOR

> 비유: 외과의사. 최소 절개, 최대 정확도. 진입도 청산도 난도질하지 않는다.

### 12.1 Almgren-Chriss 최적 집행

```python
def optimal_execution_schedule(
    total_shares: float,
    T_minutes: float,
    sigma: float,
    eta: float,
    gamma_impact: float,
    risk_aversion: float = 1e-6
) -> np.ndarray:
    """
    Almgren & Chriss (2001): 가격충격 vs 변동성 리스크 균형

    비유: 큰 코끼리가 작은 연못에서 수영할 때
    너무 빨리 들어가면 물이 튀고 (가격충격)
    너무 천천히 들어가면 날씨가 바뀜 (변동성 리스크)
    최적 속도를 계산하는 것

    kappa = √(risk_aversion × σ² / η)
    """
    n  = int(T_minutes)
    kappa = np.sqrt(risk_aversion * sigma**2 / eta)

    # 최적 거래 일정 (하이퍼볼릭 사인 기반)
    trajectory = np.array([
        total_shares * np.sinh(kappa * (T_minutes - t)) / np.sinh(kappa * T_minutes)
        for t in range(n + 1)
    ])
    return np.diff(trajectory) * -1  # 각 시점 거래량
```

### 12.2 키움 OpenAPI+ 주문 상태머신

```python
from enum import Enum, auto
import asyncio

class OrderState(Enum):
    IDLE         = auto()   # 대기
    PENDING      = auto()   # 주문 전송 중
    SUBMITTED    = auto()   # 거래소 접수
    PARTIAL_FILL = auto()   # 부분 체결
    FILLED       = auto()   # 완전 체결
    CANCELLED    = auto()   # 취소 완료
    REJECTED     = auto()   # 거부
    ERROR        = auto()   # 오류

class KiwoomOrderStateMachine:
    """
    키움 API 주문의 비동기 상태 관리
    비유: 항공기 비행 단계 관리.
          이륙(PENDING) → 상승(SUBMITTED) → 순항(PARTIAL)
          → 착륙(FILLED) or 비상착륙(CANCELLED)

    핵심 문제: 키움 API는 이벤트 기반 콜백이라
              주문 상태가 비동기적으로 업데이트됨
    """

    def __init__(self):
        self.orders: dict = {}
        self.callbacks: dict = {}

    async def place_order(self,
                           symbol: str,
                           qty: int,
                           price: float,
                           order_type: str,   # "LIMIT" | "MARKET"
                           side: str          # "BUY" | "SELL"
                           ) -> str:
        """주문 전송 및 order_id 반환"""
        order_id = self._generate_id()
        self.orders[order_id] = {
            "state":    OrderState.PENDING,
            "symbol":   symbol,
            "qty":      qty,
            "filled":   0,
            "price":    price,
            "side":     side,
            "created":  pd.Timestamp.now(),
        }
        # 키움 API 비동기 호출
        await self._send_to_kiwoom(order_id, symbol, qty, price, order_type, side)
        return order_id

    async def on_execution(self, order_id: str, filled_qty: int,
                            fill_price: float) -> None:
        """체결 콜백 처리"""
        order = self.orders[order_id]
        order["filled"] += filled_qty

        if order["filled"] >= order["qty"]:
            order["state"] = OrderState.FILLED
            await self._notify_filled(order_id, fill_price)
        else:
            order["state"] = OrderState.PARTIAL_FILL

    async def cancel_stale_orders(self, max_age_seconds: int = 30) -> list:
        """일정 시간 미체결 주문 자동 취소"""
        cancelled = []
        now = pd.Timestamp.now()
        for oid, order in self.orders.items():
            if order["state"] in [OrderState.SUBMITTED, OrderState.PARTIAL_FILL]:
                age = (now - order["created"]).total_seconds()
                if age > max_age_seconds:
                    await self._cancel_order(oid)
                    cancelled.append(oid)
        return cancelled
```

### 12.3 Adaptive Exit Engine (다층 청산)

```python
class AdaptiveExitEngine:
    """
    청산은 단순 손절/익절이 아니라
    '기대값이 감소하는 순간' 즉시 청산

    6개 청산 레이어 (우선순위 순):
    """

    EXIT_LAYERS = {
        1: "HARD_STOP",      # 절대 손실 한도 (무조건)
        2: "FORCED_FLAT",    # 15:10 강제청산 (무조건)
        3: "STRUCTURE_STOP", # VWAP/POC/GammaWall 이탈
        4: "FLOW_STOP",      # 외국인 역전 / Microprice 역행
        5: "BELIEF_DECAY",   # Meta 확률 임계 이하
        6: "TIME_STOP",      # 예상 속도 미달
    }

    REGIME_EXIT_PARAMS = {
        "TREND_UP_STRONG": {
            "trailing_stop":  0.015,   # 고점 대비 1.5% 트레일링
            "profit_target":  None,    # 추세가 끝날 때까지
            "time_stop_min":  120,
        },
        "RANGE_BALANCED": {
            "trailing_stop":  None,
            "profit_target":  0.012,   # 1.2%에서 빠른 익절
            "stop_loss":     -0.006,   # 0.6% 타이트 손절
            "time_stop_min":  30,
        },
        "VOL_EXPANSION": {
            "gamma_scalp":    True,    # 감마 스캘핑으로 전환
            "stop_loss":     -0.008,
            "vega_target":    0.30,    # Vega 목표 달성 시 청산
        },
    }

    def ev_decay_check(self,
                        position: dict,
                        current_regime: str,
                        current_flow: dict,
                        current_greeks: dict) -> dict:
        """
        1분마다 기대값 재계산
        EV = P(win)×E(win) - P(loss)×E(loss) - Theta_decay - Slippage_cost

        이 값이 0 이하로 떨어지면 손익과 무관하게 청산
        비유: 배가 목적지까지 갈 연료가 부족해지면
              중간에서라도 항구로 돌아가는 것
        """
        p_win    = position["entry_confidence"] * current_regime.get("trend_strength", 0.5)
        e_win    = position["profit_target"]
        p_loss   = 1 - p_win
        e_loss   = position["stop_loss"]
        theta    = current_greeks["theta"]  # 일일 감소
        ev       = p_win * e_win - p_loss * abs(e_loss) + theta / 390  # 1분당 Theta

        return {
            "ev":          ev,
            "exit_now":    ev < 0,
            "ev_trend":    "IMPROVING" if ev > position.get("last_ev", ev) else "DECLINING",
        }
```

---

## 13. ENGINE 9: SELF-LEARNING ENGINE

> 비유: 다윈의 진화론. 환경(시장)에 적응하지 못한 전략은 자동으로 도태되고, 살아남은 전략만 다음 세대로 진화한다.

### 13.1 온라인 학습 파이프라인

```python
class OnlineLearningPipeline:
    """
    배치 학습의 한계: 시장이 변해도 모델은 과거에 머문다
    온라인 학습: 매 거래 후 실시간으로 모델 업데이트

    비유: 지도를 한 번 만들고 끝내는 것(배치) vs
          GPS처럼 실시간으로 도로 변경을 반영하는 것(온라인)
    """

    def detect_concept_drift(self,
                              recent_pnl: pd.Series,
                              baseline_pnl: pd.Series,
                              method: str = "ADWIN") -> bool:
        """
        ADWIN (Adaptive Windowing): Bifet & Gavaldà (2007)
        성과 분포가 통계적으로 유의미하게 변했는지 탐지

        비유: 예보관의 예측 정확도가 갑자기 떨어졌다면
              날씨 패턴이 바뀐 것. 모델 재학습 필요.
        """
        # 두 기간의 평균 비교 (간단 구현)
        from scipy.stats import ks_2samp
        stat, p_value = ks_2samp(recent_pnl, baseline_pnl)
        return p_value < 0.05  # 분포가 유의미하게 다르면 Drift

    def update_model_incremental(self,
                                  new_features: pd.DataFrame,
                                  new_label: float) -> None:
        """
        River 라이브러리 기반 점진적 모델 업데이트
        새 거래 결과 → 즉시 모델 반영
        """
        import river.linear_model as lm
        import river.preprocessing as pp

        self.pipeline = pp.StandardScaler() | lm.LogisticRegression()
        self.pipeline.learn_one(new_features.iloc[-1].to_dict(), new_label)
```

### 13.2 Walk-Forward Optimization

```python
WALK_FORWARD_CONFIG = {
    "train_period_days":      252,    # 1년 훈련
    "test_period_days":        63,    # 3개월 검증
    "retrain_frequency_days":  21,    # 21일마다 재훈련
    "min_train_trades":       100,    # 최소 거래 수
    "max_param_combinations":  500,   # 과최적화 방지
}

"""
비유: 운전할 때 백미러(과거)를 보며 학습하면서
      앞창(미래)으로 실전 운전.

슬라이딩 윈도우:
[훈련: 1-252일] → [검증: 253-315일]
[훈련: 22-273일] → [검증: 274-336일]
...

핵심: 검증 기간은 훈련에 절대 사용하지 않는다.
"""
```

### 13.3 전략 생명주기 관리

```
전략 생명주기 (Research → Retirement):

[IDEATION]         아이디어 캡처
  조건: 학술 논문 또는 시장 관찰 기반
  산출: 가설 문서 (입력, 신호, 기대 메커니즘)
    ↓
[HYPOTHESIS]       통계적 검증
  조건: t-test p < 0.05, IC > 0.03
  산출: 신호 유효성 확인서
    ↓
[BACKTEST]         이벤트 기반 백테스트
  조건: DSR > 1.0, Calmar > 1.0, Max DD < 15%
  필수: 슬리피지 2배 스트레스 테스트 통과
  필수: Combinatorial Purged CV 통과
  산출: 검증된 파라미터 셋
    ↓
[SHADOW]           섀도우 모드 (30일)
  실시간 신호 생성, 가상 매매, 로그 기록
  조건: 실거래 동등 성과
    ↓
[MICRO_LIVE]       소규모 실거래 (30일)
  최소 사이즈로 실거래, 모델 행동 검증
  조건: 슬리피지 < 추정치 × 1.5
    ↓
[PRODUCTION]       정상 사이즈
  지속 모니터링, 드리프트 감시
    ↓
[RETIREMENT]       자동 비활성화
  트리거: OOS 성과 악화, Drift 감지, 해석 불가 손실
```

---

## 14. INTRADAY TRADING PLAYBOOK

### 14.1 장전 루틴 (08:30 - 09:00)

```
08:30  글로벌 체크
  ├─ 미국 야간 선물 방향 및 변동성
  ├─ VIX, VIX3M, MOVE Index 수준
  ├─ USDKRW 방향 (외국인 자금흐름 예측)
  ├─ USDCNH (중국발 리스크)
  └─ 당일 경제지표, 이벤트 캘린더

08:45  옵션 구조 체크
  ├─ 전일 종가 기준 GEX Map 업데이트
  ├─ Gamma Wall / Gamma Flip Level
  ├─ 높은 OI 집중 행사가 확인
  ├─ IV Rank (IVR) = (현재 IV - 52주 최저) / (52주 최고 - 최저)
  └─ VRP 현황 (IV - RV Spread)

08:55  시나리오 3개 작성
  ├─ 기본 시나리오 (확률 50%): 레짐, 진입 조건, 청산 계획
  ├─ 상승 시나리오 (확률 25%): 대응 전략
  └─ 하락 시나리오 (확률 25%): 대응 전략
```

### 14.2 장초반 루틴 (09:00 - 09:30)

```
09:00  개장 경매 후 5분 관찰
  ├─ 외국인/기관 초기 수급 방향
  ├─ VWAP 형성 위치 (상/하)
  ├─ 초기 급등락: 추세 vs 노이즈 분류
  └─ VPIN 초기값 (> 0.6이면 위험 신호)

09:05 ~  첫 진입 기회 탐색
  조건 체크리스트:
  [ ] 상위 레짐 확인됨 (15분 기준)
  [ ] 하위 레짐 정합 (1분 기준)
  [ ] 외국인 수급 방향 일치
  [ ] Gamma Wall 방향 우호
  [ ] VPIN < 0.7 (독성 낮음)
  [ ] 일중 손실 버퍼 충분 (3% 여유)
```

### 14.3 장중 루틴 (09:30 - 15:00)

```
매 1분 자동 업데이트:
  ├─ 레짐 상태 재확인 (1분/15분 정합성)
  ├─ GEX Map 실시간 업데이트
  ├─ VPIN, Queue Imbalance 모니터링
  ├─ 포지션별 EV 재계산
  ├─ Greeks 대차대조표 업데이트
  └─ 청산 레이어 순차 체크

판단 원칙:
  수익보다 가설 유효성을 먼저 확인
  손익보다 구조 훼손 여부를 먼저 본다
  '더 벌 수 있을 것 같다'는 감각을 신뢰하지 않는다
```

### 14.4 장마감 루틴 (15:00 - 15:20)

```
15:00  Charm 기반 드리프트 방향 최종 확인
15:05  모든 포지션 청산 계획 수립
15:08  부분 청산 시작 (시장충격 최소화)
15:10  강제평탄화 완료 확인 (당일청산 하드룰)
15:15  체결 품질 분석 (슬리피지 계산)
15:20  거래 로그 저장 + 연구 큐 태깅
```

### 14.5 만기일 특수 플레이북 (매월 두 번째 목요일)

```
만기일 특성:
  - Gamma Pinning 효과 극대화
  - 만기 행사가 근처 HVN 형성
  - 오후 2시 이후 급격한 Gamma 감소 → 방향 이탈 가능

만기일 전략:
  오전: Gamma Wall 영향권 내 매매 (Range)
  오후: Gamma Wall 이탈 여부 모니터링
  15:00 이후: 포지션 없는 것이 원칙

만기일 금지:
  - 신규 ATM 방향성 포지션
  - 근월 옵션 매도 포지션 보유
  - 만기 행사가 스프레드 거래 (유동성 급감)
```

---

## 15. VALIDATION & BACKTEST STANDARDS

### 15.1 검증 스택 (필수 통과 조건)

```python
VALIDATION_STACK = {
    # 1단계: 기본 검증
    "walk_forward":       {"train": 252, "test": 63, "step": 21},
    "purged_cv":          {"n_splits": 5, "embargo_pct": 0.01},
    "combinatorial_cv":   {"n_splits": 6, "n_test_splits": 2},

    # 2단계: 강건성 검증
    "monte_carlo":        {"n_simulations": 10000, "method": "path_shuffle"},
    "stress_spread":      {"spread_multiplier": 2.0},  # 스프레드 2배 가정
    "stress_slippage":    {"slip_multiplier": 2.0},    # 슬리피지 2배 가정
    "regime_segment":     True,   # 레짐별 분리 성과 분석

    # 3단계: 통계 검증
    "deflated_sharpe":    {"min_dsr": 1.0},
    "probabilistic_sr":   {"min_psr": 0.95},
    "min_sample_trades":  50,     # 최소 거래 수

    # 배포 기준 (모두 충족 필요)
    "deployment_criteria": {
        "sharpe_ratio":    1.5,
        "calmar_ratio":    1.0,
        "max_drawdown":   -0.15,
        "deflated_sr":     1.0,
        "monthly_win_pct": 0.65,
    }
}
```

### 15.2 반드시 통과해야 할 스트레스 질문

```
1. 수익이 특정 N일에만 집중되는가?
   → N일 제거 후에도 양의 기대값이 남는가?

2. 만기일 효과를 빼면 알파가 남는가?
   → 만기일 거래 제거 후 Sharpe 재계산

3. 슬리피지 2배 환경에서도 생존하는가?
   → 실전에서 모델 슬리피지는 항상 추정치를 초과

4. 장초반/점심/장후반 구간별 성과 편차가 크지 않은가?
   → 시간대 편향 = 곧 무너질 패턴

5. 손익의 80%가 하나의 전략 또는 레짐에서만 나오는가?
   → 집중 위험 = 레짐 전환 시 파멸

6. OOS 기간에 IS 성과의 60% 이상을 유지하는가?
   → 미달 시 과최적화로 판정 폐기

7. 전략 해석이 가능한가?
   → 왜 이 전략이 작동하는지 설명 불가 = 배포 금지
```

---

## 16. DATABASE & LOGGING ARCHITECTURE

### 16.1 TimescaleDB 스키마

```sql
-- ============================================================
-- 핵심 시계열 데이터
-- ============================================================

-- 1분봉 원시 데이터 (하이퍼테이블)
CREATE TABLE market_raw_1m (
    ts           TIMESTAMPTZ  NOT NULL,
    symbol       VARCHAR(20),
    open         NUMERIC(12,4),
    high         NUMERIC(12,4),
    low          NUMERIC(12,4),
    close        NUMERIC(12,4),
    volume       BIGINT,
    -- 파생 미시구조
    vwap         NUMERIC(12,4),
    vpin         NUMERIC(8,6),
    microprice   NUMERIC(12,4),
    bid_ask_sprd NUMERIC(8,4),
    queue_imbal  NUMERIC(6,4),
    buy_volume   BIGINT,
    sell_volume  BIGINT,
    -- 크로스에셋
    usdkrw       NUMERIC(10,4),
    PRIMARY KEY (ts, symbol)
);
SELECT create_hypertable('market_raw_1m', 'ts');

-- 옵션 체인 (1분, 행사가별)
CREATE TABLE option_chain_1m (
    ts           TIMESTAMPTZ  NOT NULL,
    underlying   VARCHAR(20),
    expiry       DATE,
    strike       NUMERIC(12,2),
    opt_type     CHAR(1),       -- C / P
    -- 가격
    bid          NUMERIC(10,4),
    ask          NUMERIC(10,4),
    last         NUMERIC(10,4),
    -- Greeks
    delta        NUMERIC(8,6),
    gamma        NUMERIC(10,8),
    theta        NUMERIC(8,6),
    vega         NUMERIC(8,6),
    vanna        NUMERIC(10,8),
    volga        NUMERIC(10,8),
    charm        NUMERIC(10,8),
    -- 변동성
    iv           NUMERIC(8,6),
    rv_5d        NUMERIC(8,6),
    vrp          NUMERIC(8,6),
    sabr_alpha   NUMERIC(8,6),
    sabr_rho     NUMERIC(8,6),
    sabr_nu      NUMERIC(8,6),
    -- 포지션
    oi           INTEGER,
    volume       INTEGER,
    gex_contrib  NUMERIC(18,4),  -- 이 행사가의 GEX 기여분
    PRIMARY KEY (ts, underlying, expiry, strike, opt_type)
);

-- 피처 스토어
CREATE TABLE feature_store_1m (
    ts                TIMESTAMPTZ  NOT NULL,
    symbol            VARCHAR(20),
    -- 레짐
    regime_state      SMALLINT,
    regime_probs      NUMERIC(8,6)[],   -- 8개 확률 벡터
    hurst_exp         NUMERIC(6,4),
    adx               NUMERIC(6,2),
    rv_ratio          NUMERIC(6,4),
    -- 주문흐름
    vpin              NUMERIC(6,4),
    queue_imbal       NUMERIC(6,4),
    absorption_flag   BOOLEAN,
    sweep_count_5m    SMALLINT,
    -- 볼륨 구조
    poc               NUMERIC(12,4),
    vah               NUMERIC(12,4),
    val               NUMERIC(12,4),
    hvn_below         NUMERIC(12,4),
    lvn_above         NUMERIC(12,4),
    poc_distance_pct  NUMERIC(6,4),
    -- 옵션
    atm_iv            NUMERIC(8,6),
    iv_rank           NUMERIC(6,4),
    vrp_current       NUMERIC(8,6),
    skew_25d          NUMERIC(8,6),
    total_gex         NUMERIC(18,4),
    gamma_flip        NUMERIC(12,4),
    charm_flow        NUMERIC(18,4),
    -- 수급
    foreign_net_5m    NUMERIC(18,4),
    inst_net_5m       NUMERIC(18,4),
    retail_net_5m     NUMERIC(18,4),
    smart_alignment   NUMERIC(6,4),
    PRIMARY KEY (ts, symbol)
);

-- 포트폴리오 Greeks 스냅샷
CREATE TABLE portfolio_greeks_1m (
    ts            TIMESTAMPTZ  NOT NULL PRIMARY KEY,
    port_delta    NUMERIC(18,4),
    port_gamma    NUMERIC(18,4),
    port_theta    NUMERIC(18,4),
    port_vega     NUMERIC(18,4),
    port_vanna    NUMERIC(18,4),
    port_volga    NUMERIC(18,4),
    port_charm    NUMERIC(18,4),
    nav           NUMERIC(18,4),
    daily_pnl     NUMERIC(18,4),
    var_95        NUMERIC(18,4),
    greeks_attrib JSONB   -- {delta_pnl, gamma_pnl, theta_pnl, vega_pnl, ...}
);

-- 신호 결정 로그
CREATE TABLE signal_decisions (
    decision_id   UUID         DEFAULT gen_random_uuid(),
    ts            TIMESTAMPTZ  NOT NULL,
    symbol        VARCHAR(20),
    -- 신호
    primary_signal SMALLINT,   -- -1, 0, 1
    meta_prob      NUMERIC(6,4),
    conviction     VARCHAR(20), -- NO_TRADE/SMALL/STANDARD/HIGH_CONVICTION
    regime_at_ts   SMALLINT,
    -- 입력 요약
    feature_hash   VARCHAR(64),
    top_features   JSONB,
    -- 결과
    trade_initiated BOOLEAN,
    reject_reason   VARCHAR(100),
    PRIMARY KEY (decision_id)
);

-- 거래 기록
CREATE TABLE trade_history (
    trade_id        UUID         DEFAULT gen_random_uuid(),
    strategy_id     VARCHAR(50),
    symbol          VARCHAR(20),
    option_strike   NUMERIC(12,2),
    option_expiry   DATE,
    option_type     CHAR(1),
    entry_ts        TIMESTAMPTZ,
    exit_ts         TIMESTAMPTZ,
    entry_price     NUMERIC(12,4),
    exit_price      NUMERIC(12,4),
    quantity        INTEGER,
    side            VARCHAR(5),   -- LONG/SHORT
    -- 비용
    gross_pnl       NUMERIC(18,4),
    commission      NUMERIC(18,4),
    slippage        NUMERIC(18,4),
    net_pnl         NUMERIC(18,4),
    -- 진입 시 상태
    regime_entry    SMALLINT,
    meta_prob_entry NUMERIC(6,4),
    iv_entry        NUMERIC(8,6),
    gex_entry       NUMERIC(18,4),
    -- 청산 이유
    exit_reason     VARCHAR(50),  -- TP/SL/TIME/EV_DECAY/REGIME/FORCED_FLAT
    -- Greeks 기여
    delta_pnl       NUMERIC(18,4),
    gamma_pnl       NUMERIC(18,4),
    theta_pnl       NUMERIC(18,4),
    vega_pnl        NUMERIC(18,4),
    PRIMARY KEY (trade_id)
);
```

---

## 17. MONITORING & ALERT SYSTEM

### 17.1 실시간 경보 체계

| 경보 레벨 | 트리거 | 자동 액션 |
|-----------|--------|-----------|
| 🔴 CRITICAL | 일중 손실 -3% / VPIN > 0.90 / 데이터 피드 5초 지연 | 전량 청산 + 신규 금지 |
| 🟠 HIGH | 일중 손실 -1.5% / Gamma Flip 이탈 / Regime CRISIS | 신규 금지 + 사이즈 50% |
| 🟡 MEDIUM | VPIN > 0.70 / Meta Prob 하락 / 슬리피지 급등 | 사이즈 축소 + 경보 |
| 🟢 LOW | 레짐 전환 / OI 급변 / 스큐 왜곡 | 모니터링 강화 |

### 17.2 모델 건강도 지표

```python
MODEL_HEALTH_METRICS = {
    # 성과
    "recent_hit_rate":    "최근 50거래 승률 (기준: >55%)",
    "regime_hit_rate":    "레짐별 승률 분해",
    "ev_realized_ratio":  "예측 EV vs 실현 EV 비율",

    # 실행
    "avg_slippage_ratio": "실제 / 추정 슬리피지 (기준: <1.5)",
    "fill_rate":          "주문 체결률 (기준: >85%)",
    "signal_to_trade":    "신호 → 실제 거래 전환율",

    # 드리프트
    "feature_drift_score":    "피처 분포 변화도",
    "prediction_drift_score": "예측 분포 변화도",
    "regime_accuracy_trend":  "레짐 탐지 정확도 추세",
}
```

---

## 18. DASHBOARD DESIGN

### 18.1 메인 커맨드 센터

```
┌──────────────────────────────────────────────────────────────────────┐
│  MAHDI v5 COMMAND CENTER                    2025-01-15  14:23:07    │
├─────────────────┬─────────────────┬─────────────────┬───────────────┤
│  REGIME         │  FLOW TOXICITY  │  GAMMA MAP       │  PnL TODAY   │
│  TREND_UP_STR   │  VPIN: 0.58 🟢  │  Flip: 2,540 ↓  │  +₩1.25M    │
│  Prob: 0.84     │  Imbal: +0.31   │  Wall: 2,600 🧲  │  +2.1% NAV  │
│  15m: ✅ 1m: ✅ │  Absorb: YES    │  GEX: +2.3B 🟢  │  Δ+820K Γ+  │
├─────────────────┴─────────────────┴─────────────────┴───────────────┤
│  VOLUME PROFILE                  │  IV SURFACE SNAPSHOT             │
│  2,620 ▎██████ 1.2M              │  ATM IV: 18.3%  IVR: 45        │
│  2,610 ▎███████████ 2.1M         │  25d RR: -1.2%  BF: 0.8%       │
│  2,600 ▎██████████████████ 3.9M ←POC│ VRP: +2.1%  → Vol Sell OK  │
│  2,590 ▎████ 0.6M     ← LVN     │  Skew: -12.3%  PUT RICH        │
│  2,580 ▎██████████ 2.0M ← HVN   │  Term: Contango  🟢 NORMAL     │
├──────────────────────────────────┴──────────────────────────────────┤
│  GREEKS BALANCE SHEET                                               │
│  Δ +12,500  Γ +180,000  Θ -45,000/d  ν +320,000                   │
│  Vanna -8,500  Volga +12,000  Charm -3,200                         │
│  Daily VaR(95%): -₩380,000  │  Risk Budget Used: 43%  🟢          │
├─────────────────────────────────────────────────────────────────────┤
│  ACTIVE POSITIONS                                                   │
│  K200 2600C Jan  │ Δ:+0.52 Γ:+0.8  │ EV:+₩320K │ 🟢 HOLD        │
│  K200 2550P Jan  │ Δ:-0.25 Γ:+0.4  │ EV:+₩150K │ 🟢 HOLD        │
├─────────────────────────────────────────────────────────────────────┤
│  ALERTS                          │  SCENARIO BOARD                  │
│  🟡 Theta 임계 접근 (-45K/d)     │  BASE(50%): 2,600 저항 테스트  │
│  🟢 VPIN 정상                    │  BULL(25%): 2,620 돌파          │
│  🟢 Risk Budget 정상             │  BEAR(25%): 2,580 HVN 지지     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 19. TECHNOLOGY STACK

```yaml
핵심 언어:
  Python 3.11+:   신호 생성, 연구, 브로커 오케스트레이션
  Rust (선택):    초저지연 실행 레이어 (<100μs 목표)

데이터 & 저장:
  TimescaleDB:    시계열 데이터 (PostgreSQL 확장)
  Redis:          실시간 캐싱 (레짐, Greeks 상태)
  Apache Kafka:   실시간 틱 스트리밍
  MinIO:          모델 아티팩트, 백테스트 결과

ML 프레임워크:
  XGBoost:        1차 방향 모델
  LightGBM:       고속 앙상블
  PyTorch:        LSTM, Transformer 시계열 모델
  hmmlearn:       Hidden Markov Model (레짐)
  River:          온라인 학습 (실시간 업데이트)
  scikit-learn:   Meta-Labeling, CV

옵션 수학:
  QuantLib:       옵션 가격 계산, Greeks
  py_vollib:      빠른 Greeks (C 기반)
  scipy.optimize: SABR, Heston 파라미터 최적화

백테스트:
  vectorbt:       고속 벡터화 백테스트
  backtrader:     이벤트 드리븐 백테스트

브로커 연동:
  KiwoomOpenAPI+: 실거래 (asyncio 기반)
  asyncio:        비동기 주문 상태머신

인프라:
  Docker:         컨테이너화
  Grafana:        실시간 대시보드
  Prometheus:     메트릭 수집
  Apache Airflow: 배치 워크플로우
  FastAPI:        내부 API 서버

품질 관리:
  pytest:         단위/통합 테스트
  great_expectations: 데이터 품질 검증
```

---

## 20. SYSTEM PERFORMANCE TARGETS

```
지연시간 목표:
  틱 수신 → 피처 계산:    < 50ms
  피처 → 신호 생성:       < 20ms
  신호 → 주문 전송:       < 10ms
  전체 파이프라인:         < 100ms

처리량 목표:
  틱 데이터 수집:          10,000 tick/s
  피처 계산:               1,000 row/s
  신호 계산:               100 signal/s
  백테스트 (5년):          < 60초

모델 정확도:
  레짐 탐지:               > 75%
  메타 정밀도:             > 60%
  신호 승률:               > 55%

재무 목표:
  연 Sharpe:               > 1.5
  Calmar Ratio:            > 1.0
  최대 낙폭:               < 15%
  월 승률:                 > 65%
  Deflated Sharpe:         > 1.0
```

---

## 21. BUILD ROADMAP

```
Phase 1 (0-2개월): 핵심 데이터 인프라
  ├─ TimescaleDB 스키마 구축
  ├─ 키움 API 틱 수집기 + 비동기 주문 상태머신
  ├─ 옵션 체인 수집기 (Greeks 포함)
  └─ 피처 스토어 파이프라인

Phase 2 (2-4개월): 핵심 엔진 구현
  ├─ Regime Engine (HMM + Bayesian Scorer)
  ├─ Order Flow Engine (VPIN + LOB Tagger)
  ├─ Options Engine (SABR + GEX + VRP)
  └─ Greeks PnL Attribution Engine

Phase 3 (4-6개월): 신호 융합 + ML
  ├─ Feature Engineering (Fractional Diff + Triple Barrier)
  ├─ Meta-Labeling System
  ├─ Combinatorial Purged CV 프레임워크
  └─ 앙상블 모델 배포

Phase 4 (6-9개월): 실행 + 리스크
  ├─ Almgren-Chriss 집행 엔진
  ├─ Adaptive Exit (6-Layer)
  ├─ Kelly + CPPI 자본 배분
  └─ Circuit Breaker + Kill Switch

Phase 5 (9-12개월): 자가학습 + 고급 전략
  ├─ Online Learning (ADWIN + River)
  ├─ Walk-Forward 자동화
  ├─ Dispersion Trading 모듈
  └─ Variance Swap VRP 포착 전술

Phase 6 (12개월+): 확장
  ├─ 미니 KOSPI200 옵션 추가
  ├─ 야간 선물 신호 통합
  └─ 대체 데이터 (뉴스 NLP, 공시 자동분석)
```

---

## 22. OPERATIONAL GOVERNANCE

### 22.1 실전 금지 목록

```
절대 금지:
  ✗ 손실 후 평균단가 낮추기 (Averaging Down)
  ✗ 시장가 추격 중독 (Chasing)
  ✗ 장마감 직전 희망성 홀딩
  ✗ 검증 안 된 신호의 즉시 실전 투입
  ✗ 이벤트 직전 무방비 방향 베팅
  ✗ Circuit Breaker 수동 해제 (개입 금지)
  ✗ 동일 가설 3회 이상 반복 손실 후 재진입

강력 주의:
  ⚠ 장 초반 10분 내 최대 사이즈 진입
  ⚠ 만기일 ATM 방향 베팅
  ⚠ VPIN > 0.7 구간 신규 진입
  ⚠ 점심 시간대 유동성 공백 진입
```

### 22.2 챔피언-도전자 모델 운영

```
Champion Model: 실거래 운영 중 (안정성 우선)
Challenger Model: Shadow Mode (30일 검증 중)

전환 조건:
  Challenger의 30일 Shadow Sharpe > Champion × 1.2
  AND Challenger Max DD < Champion Max DD
  AND 코드 리뷰 + 로직 검토 완료

전환 절차:
  1. Champion 비중 50% 유지
  2. Challenger 비중 50% 시작
  3. 2주 후 성과 비교
  4. Challenger 우세 시 100% 전환
```

---

## 23. FINAL DECLARATION

```
MAHDI v5의 본질:

시장을 맞히려는 시스템이 아니라,
구조적으로 유리할 때만 위험을 꺼내 쓰는 시스템.

르네상스 테크놀로지의 수학적 엄밀함,
Two Sigma의 데이터 과학,
AQR의 리스크 규율,
D.E. Shaw의 미시구조 이해,
Nassim Taleb의 비선형 사고,

그리고 한국 옵션시장 현장의 날카로운 현실 감각을

하나의 진화하는 지성으로 통합한

"디지털 헤지펀드 매니저"다.

----

우리가 통제할 수 없는 것: 시장의 방향
우리가 통제할 수 있는 것: 위험의 크기, 진입의 질, 청산의 규율

시장은 예측할 수 없다.
그러나 확률은 관리할 수 있다.
그것이 MAHDI의 존재 이유다.
```

---

```
Final Operating Doctrine:

  Read structure first.
  Trade only when structure, flow, and options align.
  Size smaller than your ego wants.
  Exit earlier than your hope demands.
  Flatten before the market closes.
  Review every trade as a researcher, not a gambler.
  Let the system evolve. Never let it sleep.
```

---

*MAHDI v5.0 | The Definitive Hedge Fund Blueprint*  
*Academic Rigor × Hedge Fund Discipline × Korean Market Reality*  
*"우리는 틀릴 수 있다. 그러나 틀릴 때 작게 잃고, 맞을 때 크게 번다."*
