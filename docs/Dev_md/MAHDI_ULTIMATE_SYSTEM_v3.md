# 🧠 PROJECT MAHDI — ULTIMATE INTEGRATED TRADING SYSTEM
## Version 3.0 · Hedge Fund Intelligence Edition

> **"시장은 구조이고, 수익은 확률이며, 전략은 생명체처럼 진화한다"**
> Built on: Two Sigma · Renaissance · D.E. Shaw Architecture + Academic Alpha Research

---

## 📐 SYSTEM PHILOSOPHY

### 핵심 공리 (Axioms)

```
Axiom 1: Market Microstructure > Price Action
Axiom 2: Order Flow = Truth. Price = Lagging Indicator
Axiom 3: Options Market = Smart Money's Balance Sheet
Axiom 4: Regime Awareness > Individual Signal Strength
Axiom 5: Kelly Criterion > Intuition. Always.
```

### 이론적 기반

| 분야 | 논문/이론 | 적용 |
|------|-----------|------|
| 시장 미시구조 | Kyle (1985) - Continuous Auctions | 주문흐름 독성 측정 |
| 정보 비대칭 | Glosten-Milgrom (1985) | 스프레드 분해 |
| 변동성 | Heston (1993) SV Model | IV Surface Calibration |
| 레짐 탐지 | Hamilton (1989) HMM | 시장 상태 분류 |
| 포트폴리오 | Black-Litterman (1990) | 포지션 사이징 |
| 팩터 모델 | Fama-French (1993, 2015) | Alpha Decomposition |
| 머신러닝 | Marcos Lopez de Prado (2018) | Meta-Labeling, Purged CV |

---

## 🏗️ ARCHITECTURE OVERVIEW

```
┌─────────────────────────────────────────────────────────────────┐
│                    MAHDI COMMAND CENTER                         │
│                                                                 │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐       │
│  │  REGIME  │  │  ORDER   │  │ OPTIONS  │  │  RISK    │       │
│  │  ENGINE  │  │  FLOW    │  │  INTEL   │  │  BRAIN   │       │
│  │   v3.0   │  │  ENGINE  │  │  ENGINE  │  │  ENGINE  │       │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘       │
│       │              │              │              │             │
│  ┌────▼──────────────▼──────────────▼──────────────▼────┐       │
│  │              SIGNAL FUSION LAYER (ML Ensemble)        │       │
│  │         [XGBoost + LSTM + Transformer + HMM]          │       │
│  └───────────────────────┬───────────────────────────────┘       │
│                          │                                       │
│  ┌───────────────────────▼───────────────────────────────┐       │
│  │           META-LABELING FILTER (Lopez de Prado)        │       │
│  │     Primary Signal → Meta Model → Final Conviction     │       │
│  └───────────────────────┬───────────────────────────────┘       │
│                          │                                       │
│  ┌───────────────────────▼───────────────────────────────┐       │
│  │              EXECUTION & POSITION ENGINE               │       │
│  │   [Kelly Sizing] [TWAP/VWAP] [Adaptive Exit]          │       │
│  └───────────────────────────────────────────────────────┘       │
└─────────────────────────────────────────────────────────────────┘
```

---

## 🌐 ENGINE 1: REGIME ENGINE v3.0

> 비유: 날씨예보관. 단순히 "비가 온다"가 아니라 "저기압 시스템의 이동 속도와 강수 확률 분포"를 계산한다.

### 1.1 Multi-Layer Regime Classification

```python
class RegimeEngine:
    """
    레짐 = 시장의 DNA 상태
    HMM(은닉 마르코프 모델)로 관측 불가능한 시장 상태를 추론
    """
    
    REGIMES = {
        0: "TREND_STRONG",        # 강한 추세 (모멘텀 전략 최적)
        1: "TREND_WEAK",          # 약한 추세 (신중한 추세추종)
        2: "RANGE_TIGHT",         # 좁은 레인지 (마켓메이킹 최적)
        3: "RANGE_WIDE",          # 넓은 레인지 (Mean Reversion)
        4: "VOL_EXPANSION",       # 변동성 팽창 (방어적 포지션)
        5: "VOL_COMPRESSION",     # 변동성 압축 (브레이크아웃 대기)
        6: "CRISIS",              # 위기 상태 (전략 셧다운)
        7: "LIQUIDITY_CRUNCH",    # 유동성 부족 (규모 축소)
    }
```

### 1.2 Regime Detection Signals

| 신호 | 계산 방법 | 해석 |
|------|-----------|------|
| **Hurst Exponent** | R/S Analysis | H>0.6: 추세, H<0.4: 평균회귀 |
| **ADX (Wilder)** | DMI 기반 | >25: 추세 확정 |
| **Realized Volatility Ratio** | RV(5d)/RV(20d) | >1.3: 변동성 팽창 |
| **Correlation Regime** | Rolling Corr Matrix | 급등→상관관계 수렴 = 위기 |
| **VIX Term Structure** | VIX3M/VIX | <1: Contango, >1: Backwardation |
| **Market Breadth** | A/D Line, NH-NL | 시장 참여폭 측정 |

```python
def calculate_hurst_exponent(price_series: pd.Series, lags: range) -> float:
    """
    허스트 지수: 시장의 '기억력'을 측정
    0.5 = 랜덤워크 (효율적 시장)
    >0.5 = 추세 지속 경향
    <0.5 = 평균 회귀 경향
    """
    tau = [np.std(np.subtract(price_series[lag:], price_series[:-lag])) 
           for lag in lags]
    poly = np.polyfit(np.log(lags), np.log(tau), 1)
    return poly[0]  # Hurst Exponent

def detect_regime_hmm(features: np.ndarray, n_states: int = 8) -> int:
    """Hidden Markov Model로 레짐 탐지"""
    from hmmlearn import hmm
    model = hmm.GaussianHMM(n_components=n_states, covariance_type="full")
    model.fit(features)
    return model.predict(features)[-1]
```

### 1.3 Cross-Asset Regime Confirmation

```
KOSPI 레짐 확인 시 반드시 확인할 크로스셋 지표:

USD/KRW 변화율     → 외국인 자금흐름의 선행지표
US VIX             → 글로벌 리스크온/오프
EMBI Spread        → 신흥국 신용 리스크
Copper/Gold Ratio  → 경기 민감도 (닥터 코퍼)
USDCNH             → 중국 자금흐름 (KOSPI와 상관 0.75+)
US 10Y-2Y Spread   → 경기 선행 (역전=위험신호)
```

---

## 🔬 ENGINE 2: ORDER FLOW INTELLIGENCE ENGINE

> 비유: 법의학자. 시체(캔들차트)를 보는 게 아니라, 사망 원인(주문흐름)을 해부한다.

### 2.1 VPIN (Volume-Synchronized Probability of Informed Trading)

```python
def calculate_vpin(volume: pd.Series, buy_vol: pd.Series, 
                   bucket_size: int = 50) -> pd.Series:
    """
    Easley et al. (2012) - Flash Crash 예측 논문
    VPIN = 주문 독성의 실시간 측정값
    
    - VPIN > 0.7: 정보거래자 활성화 (방향성 베팅 주의)
    - VPIN 급등: 시장 불안정성 선행 신호
    """
    sell_vol = volume - buy_vol
    vpin_values = []
    
    for i in range(0, len(volume), bucket_size):
        bucket = slice(i, i + bucket_size)
        order_imbalance = abs(buy_vol[bucket].sum() - sell_vol[bucket].sum())
        total_vol = volume[bucket].sum()
        vpin_values.append(order_imbalance / total_vol if total_vol > 0 else 0)
    
    return pd.Series(vpin_values)
```

### 2.2 Market Impact Model (Almgren-Chriss)

```python
def optimal_execution_schedule(
    shares: float,
    T: float,           # 집행 시간 (분)
    sigma: float,       # 변동성
    eta: float,         # 임시 시장 충격 계수
    gamma: float,       # 영구 시장 충격 계수
    risk_aversion: float
) -> np.ndarray:
    """
    Almgren-Chriss (2001): 최적 거래 집행 스케줄
    
    비유: 큰 코끼리가 작은 연못에서 수영할 때
    얼마나 천천히 들어가야 물을 튀기지 않는지 계산
    
    Return: 각 시간구간별 최적 매도량
    """
    # TWAP vs 최적화 스케줄 비교
    n_steps = int(T)
    kappa = np.sqrt(risk_aversion * sigma**2 / eta)
    
    schedule = np.array([
        shares * np.sinh(kappa * (T - t)) / np.sinh(kappa * T)
        for t in range(n_steps)
    ])
    return np.diff(np.append(schedule, 0)) * -1
```

### 2.3 Volume Profile Advanced

```python
class VolumeProfileEngine:
    """
    거래량 프로파일 = 시장의 히트맵
    어느 가격대에서 얼마나 싸웠는지 기록
    """
    
    def calculate_volume_at_price(self, 
                                   df: pd.DataFrame,
                                   bins: int = 100) -> dict:
        """VAP: 가격대별 거래량 분포"""
        
    def find_poc(self) -> float:
        """Point of Control: 가장 많이 거래된 가격 = 공정가치"""
        
    def find_value_area(self, threshold: float = 0.70) -> tuple:
        """Value Area High/Low: 전체 거래량의 70%가 발생한 구간"""
        # TPO (Time Price Opportunity) 이론 기반
        
    def identify_hvn_lvn(self) -> dict:
        """
        HVN (High Volume Node): 지지/저항으로 작용
        LVN (Low Volume Node): 빠른 통과 구간 = 가속 구간
        
        비유: HVN = 교통체증 구간 (속도 느림)
              LVN = 고속도로 직선 (속도 빠름)
        """
        
    def detect_absorption(self, 
                           price: float, 
                           volume_spike_ratio: float = 3.0) -> bool:
        """
        거래량 흡수 감지: 대량 매도에도 가격 안 떨어짐 = 강한 매수 흡수
        → 기관의 조용한 축적 신호
        """
```

### 2.4 주체별 포지션 트래킹 (한국 시장 특화)

```python
class InstitutionalTracker:
    """
    외국인 = 정보를 가진 큰손
    기관 = 분기 실적에 묶인 큰손  
    개인 = 감정으로 거래하는 역지표
    """
    
    def calculate_entity_vwap(self, 
                               entity: str,  # 'foreign', 'institution', 'retail'
                               lookback_days: int = 20) -> float:
        """각 주체의 평균 매입단가 추정"""
        
    def trap_detection(self) -> dict:
        """
        Bull Trap: 개인이 사면 기관이 팜
        Bear Trap: 개인이 팔면 외국인이 삼
        
        if retail_long_ratio > 0.7 and foreign_net < -threshold:
            return {"type": "BULL_TRAP", "confidence": high}
        """
        
    def smart_money_flow(self) -> float:
        """
        Chaikin Money Flow (CMF) 변형
        기관+외국인 자금흐름만 분리 계산
        """
```

---

## 📊 ENGINE 3: OPTIONS INTELLIGENCE ENGINE

> 비유: 기상위성. 주식시장이 날씨라면, 옵션시장은 기압 위성사진이다. 폭풍이 오기 전에 이미 보인다.

### 3.1 IV Surface Calibration (Heston Model)

```python
class HestonModel:
    """
    Heston (1993): 변동성이 확률적으로 변하는 옵션 가격 모델
    
    dS = μS dt + √V · S · dW₁
    dV = κ(θ - V)dt + σᵥ√V · dW₂
    
    파라미터:
    - κ: 평균회귀 속도 (변동성이 얼마나 빨리 안정되는가)
    - θ: 장기 변동성 (변동성의 '집')
    - σᵥ: 변동성의 변동성 (vol-of-vol)
    - ρ: 가격-변동성 상관관계 (레버리지 효과)
    """
    
    def calibrate(self, market_iv_surface: pd.DataFrame) -> dict:
        """시장 IV Surface에 Heston 파라미터 피팅"""
        
    def price_option(self, S, K, T, r, params) -> float:
        """특성함수 기반 반해석적 가격 계산"""
```

### 3.2 Gamma Exposure (GEX) Analysis

```python
class GammaExposureEngine:
    """
    GEX = 딜러들이 헷징하기 위해 움직여야 하는 주식 물량
    
    Positive GEX: 딜러가 시장 안정화 역할 (변동성 억제)
    Negative GEX: 딜러가 시장 증폭 역할 (변동성 폭발)
    
    비유: GEX > 0 = 댐이 있는 강 (잔잔)
          GEX < 0 = 댐이 없는 강 (급류)
    """
    
    def calculate_gex(self, options_chain: pd.DataFrame) -> float:
        """
        GEX = Σ (Gamma × OI × 100 × S²/100) for calls
            - Σ (Gamma × OI × 100 × S²/100) for puts
        """
        
    def find_gamma_flip(self) -> float:
        """
        Gamma Flip Level: GEX가 양에서 음으로 바뀌는 가격대
        → 이 레벨 이탈 시 변동성 폭발 가능성 급등
        """
        
    def gamma_wall(self) -> list:
        """
        Gamma Wall: 가장 큰 Gamma가 집중된 행사가
        → 가격을 잡아당기는 자석 역할 (Pinning Effect)
        
        만기일 가까울수록 Gamma Wall 효과 극대화
        """
        
    def calculate_charm(self, options_chain: pd.DataFrame) -> float:
        """
        Charm (Delta Decay): 시간이 지남에 따른 Delta 변화
        → 딜러의 일중 리밸런싱 방향 예측
        
        장 마감 전 1-2시간: Charm 방향으로 가격 드리프트
        """
```

### 3.3 Volatility Risk Premium (VRP)

```python
def calculate_vrp(iv: float, rv: float, lookback: int = 20) -> float:
    """
    VRP = IV - RV (실현변동성)
    
    VRP > 0: 옵션이 비싸다 → 옵션 매도 유리 (Premium Selling)
    VRP < 0: 옵션이 싸다 → 옵션 매수 유리 (Tail Hedging)
    
    학술 근거: Carr & Wu (2009) - Variance Risk Premiums
    평균적으로 VRP > 0 (옵션 매도자가 장기적으로 유리)
    """
    return iv - rv

def calculate_skew_index(puts_iv: pd.Series, calls_iv: pd.Series) -> float:
    """
    SKEW Index: 꼬리 위험에 대한 시장의 공포도
    
    높은 Skew = 풋옵션이 비쌈 = 하락 공포 高
    낮은 Skew = 콜옵션이 비쌈 = 상승 탐욕 高
    
    CBOE SKEW > 130: 블랙스완 경계
    """
```

### 3.4 Put-Call Ratio Intelligence

```python
class PCRAnalysis:
    """
    PCR = 풋 거래량 / 콜 거래량
    
    역설적 지표: 
    PCR 극단적 高 = 공포 극대 = 역발상 매수 신호
    PCR 극단적 低 = 탐욕 극대 = 역발상 매도 신호
    
    단, OI 기준 PCR와 Volume 기준 PCR를 분리해서 분석!
    """
    
    def smart_pcr(self) -> dict:
        """기관 전용 대형 옵션 PCR (스마트머니 추적)"""
```

---

## 🤖 ENGINE 4: ML SIGNAL FUSION ENGINE

> 비유: 오케스트라 지휘자. 각 악기(엔진)의 소리를 받아 하모니(최종 신호)를 만든다.

### 4.1 Feature Engineering (Lopez de Prado 방법론)

```python
class FeatureEngineering:
    """
    Advances in Financial Machine Learning (2018) 기반
    """
    
    def fractional_differentiation(self, series: pd.Series, d: float) -> pd.Series:
        """
        분수 차분: 정상성(stationarity)과 기억력(memory)의 균형
        
        d=1: 완전 차분 (기억력 제거, ML에 안전하지만 정보 손실)
        d=0.3~0.5: 최적 (정상성 유지 + 장기 기억력 보존)
        
        비유: 빨래할 때 너무 세게 짜면 형태가 망가지고
              너무 약하게 짜면 물이 남는다. 최적 강도를 찾는 것.
        """
        
    def triple_barrier_labeling(self,
                                 df: pd.DataFrame,
                                 pt: float,    # 익절 배리어
                                 sl: float,    # 손절 배리어  
                                 t1: pd.Series # 시간 배리어
                                 ) -> pd.Series:
        """
        3중 배리어 레이블링: 어느 배리어에 먼저 닿는가?
        1 = 익절 (상단 배리어)
        -1 = 손절 (하단 배리어)
        0 = 시간 만료 (수직 배리어)
        """
        
    def purged_kfold_cv(self, n_splits: int = 5, pct_embargo: float = 0.01):
        """
        금융 데이터의 누수(Leakage) 방지 교차검증
        
        일반 K-Fold의 문제: 미래 데이터가 훈련에 포함됨
        Purged K-Fold: 테스트 기간 앞뒤를 제거하여 누수 차단
        """
```

### 4.2 Meta-Labeling (2차 모델)

```python
class MetaLabelingModel:
    """
    Lopez de Prado의 핵심 혁신:
    
    1차 모델: 방향성 예측 (매수 or 매도)
    2차 모델: 1차 모델의 예측을 믿을지 말지 결정
    
    비유: 
    1차 모델 = 예측가 ("내일 비 올 것 같아")
    2차 모델 = 팩트체커 ("그 예측가의 정확도가 얼마나 되지?")
    """
    
    def train_meta_model(self,
                          primary_signals: pd.DataFrame,
                          market_features: pd.DataFrame,
                          labels: pd.Series) -> None:
        """
        Features: 1차 모델 신호 + 레짐 상태 + 시장 특성
        Labels: 1차 모델이 맞았는지 여부 (0 or 1)
        
        Output: 각 신호의 신뢰도 (0~1)
        """
        from sklearn.ensemble import RandomForestClassifier
        # Bet size = signal_direction × meta_model_confidence
```

### 4.3 Ensemble Architecture

```python
class SignalEnsemble:
    """
    개별 모델 = 각기 다른 렌즈
    앙상블 = 모든 렌즈를 합친 복합 망원경
    """
    
    MODELS = {
        "regime_hmm": {"weight": 0.20, "type": "regime_aware"},
        "xgboost_momentum": {"weight": 0.20, "type": "ml_tabular"},
        "lstm_temporal": {"weight": 0.20, "type": "deep_learning"},
        "transformer_attention": {"weight": 0.15, "type": "deep_learning"},
        "options_flow": {"weight": 0.15, "type": "options"},
        "order_flow_vpin": {"weight": 0.10, "type": "microstructure"},
    }
    
    def dynamic_weight_allocation(self, recent_performance: dict) -> dict:
        """
        고정 가중치 X → 최근 성과 기반 동적 가중치
        잘 맞추고 있는 모델에 더 많은 가중치
        """
```

---

## 💰 ENGINE 5: RISK & POSITION SIZING ENGINE

> 비유: 항공기 안전시스템. 자동조종(전략)은 파일럿이지만, 실속 경보와 지형충돌 회피는 별도 시스템이 독립적으로 작동한다.

### 5.1 Kelly Criterion (수정판)

```python
def fractional_kelly(
    win_prob: float,
    win_loss_ratio: float,
    fraction: float = 0.25  # Full Kelly의 25% (Half Kelly 아닌 Quarter Kelly)
) -> float:
    """
    Kelly (1956): 기하급수적 성장을 극대화하는 최적 베팅 크기
    
    Full Kelly = (p*b - q) / b
    
    실전에서 Full Kelly는 변동성이 너무 크므로 1/4 Kelly 사용
    
    비유: 풀 켈리 = 100% 실력 발휘 (부상 위험 高)
          쿼터 켈리 = 75% 실력 발휘 (안정적 장기전)
    
    출력: 포트폴리오 대비 포지션 비율
    """
    p = win_prob
    q = 1 - p
    b = win_loss_ratio
    
    full_kelly = (p * b - q) / b
    return max(0, full_kelly * fraction)
```

### 5.2 Dynamic Risk Budget

```python
class RiskBudgetEngine:
    """
    AQR / Bridgewater 방식의 리스크 예산 배분
    수익이 아닌 '리스크'를 균등 배분
    """
    
    MAX_PORTFOLIO_VAR = 0.02      # 일일 최대 손실 2%
    MAX_SINGLE_TRADE_VAR = 0.005  # 단일 트레이드 최대 손실 0.5%
    MAX_CORRELATION_EXPOSURE = 3   # 동일 방향 최대 동시 포지션
    MAX_DRAWDOWN_HALT = 0.05      # 5% 드로우다운 시 자동 거래 중단
    
    def calculate_position_size(self,
                                 signal_confidence: float,
                                 current_volatility: float,
                                 regime: str,
                                 portfolio_heat: float) -> float:
        """
        포지션 크기 = Kelly 사이즈 × 레짐 배수 × 변동성 역수 × (1 - 포트폴리오 열기)
        """
        
    def volatility_targeting(self, 
                              target_vol: float = 0.15,
                              realized_vol: float = None) -> float:
        """
        AHL / Man AHL 방식: 변동성 목표치 유지
        변동성 높을 때 = 포지션 축소
        변동성 낮을 때 = 포지션 확대
        """
        return target_vol / realized_vol  # scaling factor
```

### 5.3 Correlation & Concentration Risk

```python
def calculate_portfolio_heat(positions: list, 
                               corr_matrix: pd.DataFrame) -> float:
    """
    상관관계 조정 포트폴리오 위험 계산
    
    단순히 포지션 개수가 많아도 상관관계 높으면 분산 효과 없음
    
    비유: 우산 5개를 들고 가도 비는 똑같이 맞는다.
          상관관계 높은 5개 포지션 = 우산 5개 = 리스크 분산 착각
    """
```

---

## 🎯 ENGINE 6: EXECUTION & EXIT ENGINE

### 6.1 옵션 진입 전략 매트릭스

```
┌─────────────────────────────────────────────────────────────────────┐
│              OPTIONS STRATEGY SELECTION MATRIX                      │
│                                                                     │
│  레짐 \ IV 수준    │  IV 낮음(VRP<0) │  IV 적정     │  IV 높음(VRP>0) │
│ ─────────────────┼───────────────┼─────────────┼──────────────── │
│  TREND_STRONG    │  ATM Long     │  ITM Debit  │  Ratio Spread   │
│                  │  더 많이 사라 │  Spread     │  Cost 절감      │
│ ─────────────────┼───────────────┼─────────────┼──────────────── │
│  RANGE_TIGHT     │  OTM Strangle │  Iron       │  Short Strangle │
│                  │  Buy          │  Condor     │  Premium 수집   │
│ ─────────────────┼───────────────┼─────────────┼──────────────── │
│  VOL_EXPANSION   │  ATM Straddle │  Defensive  │  Short Gamma    │
│                  │  Buy          │  Only       │  피할 것        │
│ ─────────────────┼───────────────┼─────────────┼──────────────── │
│  VOL_COMPRESSION │  Straddle     │  Wait for   │  Sell Vol       │
│                  │  Buy (대기)   │  Breakout   │  Structures     │
└─────────────────────────────────────────────────────────────────────┘
```

### 6.2 Adaptive Exit Engine (핵심 강화)

```python
class AdaptiveExitEngine:
    """
    Avellaneda & Lee (2010): 통계적 차익거래 청산 이론 기반
    
    청산은 단순한 손절/익절이 아닌
    '기대값이 감소하는 순간' 즉시 청산
    """
    
    EXIT_RULES = {
        "TREND_STRONG": {
            "trailing_stop": True,
            "trail_pct": 0.015,           # 고점 대비 1.5% 트레일링
            "profit_target": None,         # 트렌드가 끝날 때까지 보유
            "time_stop": 120,              # 120분 최대 보유
            "theta_decay_exit": False,
        },
        "RANGE_TIGHT": {
            "trailing_stop": False,
            "profit_target": 0.015,        # 1.5%에서 빠른 청산
            "stop_loss": -0.008,           # 0.8% 손절 (타이트하게)
            "time_stop": 30,               # 레인지 → 시간이 독
            "theta_decay_exit": True,      # Theta가 불리해지면 청산
        },
        "VOL_EXPANSION": {
            "stop_loss": -0.01,            # 1% 손절 (변동성 클수록 빠르게)
            "gamma_scalping": True,        # Gamma P&L로 비용 회수
            "vega_exit": 0.3,             # Vega 목표치 달성 시 청산
        },
    }
    
    def calculate_expected_value_decay(self,
                                        position: dict,
                                        current_market: dict) -> float:
        """
        실시간 EV 모니터링: EV가 임계치 아래로 떨어지면 청산
        
        EV = P(win) × Avg_Win - P(loss) × Avg_Loss - Theta_decay
        """
        
    def gamma_scalping_trigger(self,
                                delta: float,
                                delta_threshold: float = 0.05) -> bool:
        """
        Gamma Scalping: Delta가 임계치를 넘으면 주식으로 헷지
        → 변동성 수익 실현 + 포지션 방어 동시 달성
        """
```

---

## 🧬 ENGINE 7: SELF-LEARNING ENGINE (진화 시스템)

> 비유: 다윈의 진화론을 트레이딩에 적용. 살아남은 전략만 다음 세대로.

### 7.1 Online Learning Pipeline

```python
class OnlineLearningEngine:
    """
    배치 학습의 한계: 시장이 변해도 모델은 과거에 머문다
    온라인 학습: 매 거래 후 실시간으로 모델 업데이트
    """
    
    def detect_concept_drift(self, 
                              recent_performance: pd.Series,
                              baseline_performance: pd.Series,
                              method: str = "ADWIN") -> bool:
        """
        ADWIN (Adaptive Windowing): 성과 분포 변화 감지
        
        Drift 감지 시: 모델 재학습 트리거
        비유: GPS가 도로 변경을 감지하고 경로를 재계산하는 것
        """
        
    def update_model_online(self, new_observation: dict) -> None:
        """River 라이브러리 기반 온라인 업데이트"""
```

### 7.2 Walk-Forward Optimization

```python
class WalkForwardEngine:
    """
    In-Sample: 모델 훈련 (과거 데이터)
    Out-of-Sample: 모델 검증 (미래 데이터)
    
    슬라이딩 윈도우로 지속적 재최적화
    
    비유: 운전할 때 거울로 뒤를 보면서도
          앞창으로 앞을 보는 것처럼
          과거로 학습하고 미래를 검증
    """
    
    TRAIN_PERIOD = 252    # 1년 훈련
    TEST_PERIOD = 63      # 3개월 검증
    RETRAIN_FREQUENCY = 21  # 21일마다 재훈련
```

### 7.3 Strategy Lifecycle Management

```
아이디어 생명주기 (Research → Production):

[IDEATION]           아이디어 캡처 → 학술논문/시장관찰
    ↓
[HYPOTHESIS]         통계적 검증 → t-test, p-value < 0.05
    ↓
[BACKTEST]           이벤트 기반 백테스트 → Sharpe > 1.5
    ↓                슬리피지 + 거래비용 반영
    ↓                Monte Carlo 10,000회 시뮬레이션
[PAPER TRADING]      섀도우 모드 → 실시간 신호만 생성 (30일)
    ↓
[MICRO LIVE]         최소 사이즈로 실거래 → 모델 행동 검증 (30일)
    ↓
[FULL PRODUCTION]    정상 사이즈 → 지속 모니터링
    ↓
[RETIREMENT]         성과 악화 → 자동 비활성화 트리거
```

---

## 🏦 ENGINE 8: HEDGE FUND GRADE BACKTEST ENGINE

### 8.1 Backtest Framework

```python
class MahdiBacktestEngine:
    """
    Vectorbt 기반 이벤트 드리븐 백테스트
    
    일반 백테스트의 문제점:
    1. Look-ahead Bias (미래 데이터 사용)
    2. Survivorship Bias (살아남은 종목만 분석)
    3. Overfitting (과최적화)
    
    마흐디 백테스트는 이 모든 것을 방지
    """
    
    REALISTIC_ASSUMPTIONS = {
        "slippage_model": "market_impact",  # 시장충격 모델링
        "commission": 0.00015,              # 편도 0.015% (증권사 실제 수수료)
        "borrow_cost": 0.003,               # 공매도 차입비용 연 0.3%
        "market_impact": "sqrt_model",      # √volume 비례 시장충격
        "fill_assumption": "next_bar",      # 신호 발생 다음 봉에서 체결
        "partial_fill": True,               # 부분 체결 반영
    }
    
    def deflated_sharpe_ratio(self, 
                               sharpe: float,
                               n_trials: int,
                               skewness: float,
                               kurtosis: float) -> float:
        """
        Bailey & Lopez de Prado (2014): Deflated Sharpe Ratio
        
        수백 번의 파라미터 탐색으로 높은 Sharpe가 나왔다면?
        → 행운일 가능성이 높다
        
        DSR: 통계적 유의성 조정 후의 진짜 Sharpe
        실전 기준: DSR > 1.0 이어야 의미 있음
        """
```

### 8.2 Performance Analytics Suite

```python
PERFORMANCE_METRICS = {
    # 수익성
    "cagr": "연복리 성장률",
    "sharpe_ratio": "위험조정 수익률 (>1.5 목표)",
    "sortino_ratio": "하방위험 조정 수익률 (>2.0 목표)",
    "calmar_ratio": "CAGR / Max Drawdown (>1.0 목표)",
    "omega_ratio": "임계 수익률 초과/미달 비율",
    
    # 위험
    "max_drawdown": "최대 낙폭",
    "var_95": "95% 신뢰구간 VaR",
    "cvar_95": "기대 손실 (Tail Risk)",
    "ulcer_index": "드로우다운 깊이 × 기간",
    
    # 실행 품질
    "avg_slippage": "평균 슬리피지",
    "market_impact": "평균 시장충격",
    "implementation_shortfall": "이상적 vs 실제 체결가 차이",
    
    # 전략 강건성
    "deflated_sharpe": "과최적화 조정 Sharpe",
    "probabilistic_sharpe": "Sharpe 통계적 유의성",
    "num_effective_trials": "실질 독립 시도 횟수",
}
```

---

## 🗄️ DATABASE ARCHITECTURE

### 9.1 Schema Design (TimescaleDB)

```sql
-- 1분봉 Raw Market Data (하이퍼테이블)
CREATE TABLE market_raw_1m (
    timestamp   TIMESTAMPTZ NOT NULL,
    symbol      VARCHAR(20),
    open        DECIMAL(18, 4),
    high        DECIMAL(18, 4),
    low         DECIMAL(18, 4),
    close       DECIMAL(18, 4),
    volume      BIGINT,
    -- 파생 지표
    vwap        DECIMAL(18, 4),
    vpin        DECIMAL(8, 6),    -- 주문 독성
    bid_ask_spread DECIMAL(8, 4),
    trade_count INTEGER,
    buy_volume  BIGINT,           -- 집계 매수
    sell_volume BIGINT,           -- 집계 매도
    usdkrw      DECIMAL(10, 4),
    PRIMARY KEY (timestamp, symbol)
);

-- 옵션 체인 분석 (1분 단위)
CREATE TABLE option_analysis_1m (
    timestamp   TIMESTAMPTZ NOT NULL,
    underlying  VARCHAR(20),
    expiry      DATE,
    strike      DECIMAL(18, 2),
    option_type CHAR(1),          -- C or P
    -- Greeks
    delta       DECIMAL(8, 6),
    gamma       DECIMAL(10, 8),
    theta       DECIMAL(8, 6),
    vega        DECIMAL(8, 6),
    rho         DECIMAL(8, 6),
    -- 시장 데이터
    iv          DECIMAL(8, 6),    -- Implied Volatility
    rv_5d       DECIMAL(8, 6),    -- 5일 실현변동성
    vrp         DECIMAL(8, 6),    -- VRP = IV - RV
    skew        DECIMAL(8, 6),    -- 25-delta skew
    gex         DECIMAL(18, 4),   -- Gamma Exposure
    oi          INTEGER,          -- Open Interest
    volume      INTEGER,
    PRIMARY KEY (timestamp, underlying, expiry, strike, option_type)
);

-- ML 피처 스토어
CREATE TABLE feature_store (
    timestamp       TIMESTAMPTZ NOT NULL,
    symbol          VARCHAR(20),
    -- Regime Features
    regime_state    INTEGER,
    regime_prob     DECIMAL(8, 6)[],  -- 8개 레짐 확률 벡터
    hurst_exp       DECIMAL(6, 4),
    adx             DECIMAL(6, 2),
    -- Order Flow
    vpin_signal     DECIMAL(6, 4),
    absorption_flag BOOLEAN,
    poc_distance    DECIMAL(8, 4),    -- 현재가 vs POC
    hvn_support     DECIMAL(18, 4),
    lvn_above       DECIMAL(18, 4),
    -- Options
    gex_level       DECIMAL(18, 4),
    gamma_flip      DECIMAL(18, 4),
    pcr_oi          DECIMAL(6, 4),
    iv_percentile   DECIMAL(6, 4),    -- IV Rank 0-100
    skew_index      DECIMAL(6, 4),
    PRIMARY KEY (timestamp, symbol)
);

-- 예측 로그 (모델 성과 추적)
CREATE TABLE prediction_logs (
    pred_id         UUID DEFAULT gen_random_uuid(),
    timestamp       TIMESTAMPTZ NOT NULL,
    symbol          VARCHAR(20),
    model_id        VARCHAR(50),
    prediction      SMALLINT,         -- -1, 0, 1
    confidence      DECIMAL(6, 4),    -- Meta Model 확률
    regime_at_pred  INTEGER,
    signal_features JSONB,
    -- 사후 평가
    actual_return_1h  DECIMAL(8, 6),
    actual_return_1d  DECIMAL(8, 6),
    was_correct       BOOLEAN,
    PRIMARY KEY (pred_id)
);

-- 거래 기록
CREATE TABLE trade_history (
    trade_id        UUID DEFAULT gen_random_uuid(),
    strategy_id     VARCHAR(50),
    symbol          VARCHAR(20),
    entry_time      TIMESTAMPTZ,
    exit_time       TIMESTAMPTZ,
    entry_price     DECIMAL(18, 4),
    exit_price      DECIMAL(18, 4),
    quantity        DECIMAL(18, 4),
    side            VARCHAR(10),      -- LONG/SHORT
    option_type     VARCHAR(20),      -- ATM_CALL, ITM_PUT, etc
    -- P&L
    gross_pnl       DECIMAL(18, 4),
    commission      DECIMAL(18, 4),
    slippage        DECIMAL(18, 4),
    net_pnl         DECIMAL(18, 4),
    -- 진입 시 상태
    regime_entry    INTEGER,
    confidence_entry DECIMAL(6, 4),
    -- 청산 이유
    exit_reason     VARCHAR(50),      -- TP/SL/TIME/EV_DECAY/REGIME_CHANGE
    PRIMARY KEY (trade_id)
);
```

---

## 🖥️ UI DASHBOARD

### 10.1 실시간 커맨드 센터

```
┌─────────────────────────────────────────────────────────────────┐
│  MAHDI COMMAND CENTER                           2024-01-15 14:32│
├────────────┬────────────┬────────────┬────────────┬─────────────┤
│ REGIME     │ SIGNALS    │ RISK METER │ P&L TODAY  │ GEX LEVEL   │
│ TREND_STR  │ ████ 78%   │ ███░ 45%   │ +₩1.2M     │ +2.3B       │
│ Prob: 0.82 │ BUY BIAS   │ MODERATE   │ +2.3% PF   │ STABLE MKT  │
├────────────┴────────────┴────────────┴────────────┴─────────────┤
│                    VOLUME HEATMAP                                │
│  2610│ ████████████████████ 2.3M                                │
│  2600│ ████████████████████████████████ 3.8M  ← POC            │
│  2590│ █████████████ 1.5M                                       │
│  2580│ ████ 0.5M   ← LVN (빠른 통과 구간)                      │
├─────────────────────────────────────────────────────────────────┤
│  GAMMA EXPOSURE           │  ORDER FLOW DELTA                   │
│  Gamma Flip: 2,550        │  VPIN: 0.65 (⚠ 높음)               │
│  Gamma Wall: 2,600 (Call) │  Absorption: DETECTED               │
│  GEX: +2.3B (Stable)     │  Foreign: +₩450B NET BUY           │
├─────────────────────────────────────────────────────────────────┤
│  ACTIVE POSITIONS                                               │
│  KOSPI200 2600C Mar  │ Delta: 0.52 │ EV: +₩320K │ 🟢 HOLD     │
│  KOSPI200 2550P Mar  │ Delta:-0.25 │ EV: +₩150K │ 🟢 HOLD     │
└─────────────────────────────────────────────────────────────────┘
```

---

## 🔧 TECHNOLOGY STACK

```yaml
Language:
  - Python 3.11+ (핵심 엔진)
  - Rust (초저지연 실행 레이어, 목표 < 100μs)
  - SQL (분석 쿼리)

Data & Storage:
  - TimescaleDB (시계열 데이터, PostgreSQL 기반)
  - Redis (실시간 캐싱, 레짐 상태)
  - Apache Kafka (데이터 스트리밍)
  - MinIO (모델 아티팩트 저장)

ML Framework:
  - scikit-learn (기본 ML)
  - XGBoost / LightGBM (Gradient Boosting)
  - PyTorch (LSTM, Transformer)
  - hmmlearn (Hidden Markov Model)
  - River (온라인 학습)

Options Analytics:
  - QuantLib (옵션 가격 계산)
  - py_vollib (빠른 Greeks 계산)
  - mibian (Black-Scholes)

Backtesting:
  - vectorbt (빠른 벡터화 백테스트)
  - backtrader (이벤트 드리븐)

Infrastructure:
  - Docker + Kubernetes (컨테이너화)
  - Grafana + Prometheus (모니터링)
  - Airflow (워크플로우 스케줄링)
  - FastAPI (내부 API)
```

---

## 📚 ACADEMIC FOUNDATION

### 핵심 참고 논문

| 분야 | 논문 | 핵심 기여 |
|------|------|-----------|
| **Market Microstructure** | Kyle (1985) "Continuous Auctions" | 정보 거래자 모델 |
| **Order Flow** | Easley et al. (2012) VPIN | 플래시크래시 예측 |
| **Execution** | Almgren-Chriss (2001) | 최적 거래 집행 |
| **Volatility** | Heston (1993) | 확률적 변동성 모델 |
| **Gamma** | Taleb (1997) "Dynamic Hedging" | 실전 옵션 헷징 |
| **ML in Finance** | Lopez de Prado (2018) | 금융 ML 바이블 |
| **Sharpe** | Bailey & de Prado (2014) DSR | 과최적화 탐지 |
| **Kelly** | Kelly (1956), Thorp (1969) | 최적 베팅 |
| **Regime** | Hamilton (1989) HMM | 레짐 스위칭 |
| **Risk Parity** | Qian (2005) Risk Budget | 리스크 예산 배분 |
| **Skew** | Bakshi et al. (2003) | 옵션 스큐 이론 |
| **Vol Premium** | Carr & Wu (2009) VRP | 변동성 리스크 프리미엄 |

---

## ⚡ SYSTEM PERFORMANCE TARGETS

```
지연시간 (Latency):
  신호 생성:     < 50ms
  포지션 계산:   < 10ms
  주문 전송:     < 5ms  (Rust 레이어)
  전체 파이프:   < 100ms

용량 (Throughput):
  데이터 수집:   10,000 tick/s
  신호 계산:     1,000 신호/s
  백테스트:      5년 데이터 < 60초

정확도 목표:
  레짐 탐지:     > 75% 정확도
  신호 정밀도:   > 55% (수수료 초과 수익)
  메타 모델:     > 65% (신호 필터링)

재무 목표:
  연 Sharpe:     > 1.5
  최대 DD:       < 15%
  월 승률:       > 65%
  CAGR:          > 25%
```

---

## 🚨 CIRCUIT BREAKERS & KILL SWITCH

```python
class CircuitBreaker:
    """
    자동 거래 중단 시스템
    비유: 전기 차단기. 과부하가 걸리면 자동으로 전원 차단.
    """
    
    HALT_CONDITIONS = {
        "daily_loss_pct": -0.03,         # 일일 -3% 손실
        "weekly_loss_pct": -0.05,        # 주간 -5% 손실
        "max_drawdown_pct": -0.10,       # 최대 낙폭 -10%
        "model_drift_detected": True,    # 드리프트 감지
        "liquidity_crisis": True,        # 유동성 위기 (VPIN > 0.9)
        "correlation_breakdown": True,   # 상관관계 붕괴
        "vix_spike": 40,                 # VIX 40 돌파
        "usdkrw_daily_change": 0.02,    # 환율 2% 이상 변동
    }
    
    def emergency_flatten(self) -> None:
        """모든 포지션 즉시 청산 (시장가)"""
        
    def gradual_delever(self, target_exposure: float) -> None:
        """목표 익스포저까지 단계적 축소 (충격 최소화)"""
```

---

## 🔮 ROADMAP

```
Phase 1 (현재): 핵심 엔진 구축
├── Regime Engine (HMM + Cross-Asset)
├── Order Flow Engine (VPIN + VAP)
└── Options Engine (GEX + VRP)

Phase 2 (3개월): ML 통합
├── Feature Store 구축
├── Meta-Labeling 구현
└── 앙상블 모델 배포

Phase 3 (6개월): 자가학습
├── Online Learning 파이프라인
├── Walk-Forward 자동화
└── Research → Production 자동 파이프

Phase 4 (12개월): 글로벌 확장
├── US Options Market 추가
├── Cross-Market 차익거래
└── Alternative Data 통합 (뉴스 NLP, 위성 데이터)
```

---

## 🏆 FINAL DECLARATION

```
마흐디는 단순한 알고리즘 트레이딩 시스템이 아니다.

시장 미시구조의 물리학,
옵션의 수학,
머신러닝의 통계학,
행동경제학의 심리학을

하나의 통합 지성으로 융합한

"디지털 헤지펀드 매니저"다.

르네상스 테크놀로지의 수학적 엄밀함,
Two Sigma의 데이터 과학,
AQR의 리스크 관리,
그리고 Nassim Taleb의 불확실성 철학을

모두 내재화한 시스템.

시장은 예측할 수 없다.
그러나 확률은 관리할 수 있다.
그것이 마흐디의 존재 이유다.
```

---

*Version 3.0 | Built with Academic Rigor + Hedge Fund Discipline*
*"우리는 틀릴 수 있다. 그러나 틀릴 때 작게 잃고, 맞을 때 크게 번다."*
