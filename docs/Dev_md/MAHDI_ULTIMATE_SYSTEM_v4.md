# MAHDI ULTIMATE SYSTEM v4
## Korea Options Hedge Fund Master Blueprint

> "시장은 소음처럼 보이지만, 실제로는 구조와 확률과 자금의 언어로 말한다."
> 
> Built for: KOSPI200 Options · Intraday Intelligence · Same-Day Risk Closure · Hedge Fund Grade Decision Stack

---

## 0. Executive Summary

MAHDI v4는 KOSPI200 옵션시장을 중심으로 설계된 종합 옵션 트레이딩 청사진이다.

이 문서는 단순한 방향 예측 모델 설명서가 아니다. 레짐 인식, 주문흐름 해석, 옵션 미시구조 분석, 포트폴리오 리스크 통제, 실행 알고리즘, 자가학습, 검증 체계, 운용 대시보드까지 하나의 운영체계로 묶어낸 마스터 블루프린트다.

핵심 목표는 다음 네 가지다.

1. 하루 안에서 구조적으로 우세한 거래만 선별한다.
2. 방향성만이 아니라 변동성, 감마, 유동성, 수급 왜곡까지 수익화한다.
3. 당일 청산 절대 원칙 아래에서 손실 꼬리를 짧게 유지한다.
4. 감에 의존하지 않고 데이터, 검증, 리스크 규율로 시스템을 진화시킨다.

이 설계는 헤지펀드의 실전 운용 방식과 학계에서 검증된 주요 프레임워크를 한국 옵션시장 현실에 맞게 재조합한 것이다. 수익을 약속하지는 않지만, 생존성과 재현성을 우선하는 상위권 트레이딩 시스템이 가져야 할 설계 원칙을 최대한 명료하게 반영한다.

---

## 1. System Philosophy

### 1.1 핵심 공리

```text
Axiom 1. Price is the shadow. Order flow is the cause.
Axiom 2. 옵션 시장은 단순한 파생상품 시장이 아니라, 스마트머니의 리스크 대차대조표다.
Axiom 3. 강한 신호보다 중요한 것은 그 신호가 어떤 레짐에서 발생했는가이다.
Axiom 4. 높은 기대수익보다 먼저 확보해야 할 것은 낮은 파산확률이다.
Axiom 5. 모든 진입은 가설이고, 청산은 검증이다.
Axiom 6. 좋은 전략은 예측률보다 손익비, 손실 비대칭, 반복 가능성에서 드러난다.
Axiom 7. 당일 청산 원칙은 성과 제약이 아니라 생존 프리미엄이다.
```

### 1.2 시스템 비유

- 나침반: 오늘 시장이 추세장인지, 레인지장인지, 변동성 팽창 국면인지 판단한다.
- 레이더: 1분마다 수급, 옵션 체인, 주문 독성, 감마 지형 변화를 탐지한다.
- 항해사: 탐지된 신호를 그대로 믿지 않고 레짐과 리스크 예산 안에서 행동으로 변환한다.
- 관제센터: 현재 포지션, Greeks, 손익, 강제청산 기준, 시스템 건강도를 한 화면에서 통제한다.
- 연구소: 오늘의 거래가 내일의 규칙이 될 수 있는지 백테스트와 실거래 로그로 검증한다.

### 1.3 성공 기준

- 높은 승률보다 손실 분포 통제
- 단일 대박보다 누적 기대값 우위
- 과최적화보다 견고한 일반화
- 직관보다 규칙 우선
- 시그널 수보다 실행 품질 우선

---

## 2. Academic and Institutional Foundations

### 2.1 핵심 이론 지도

| 분야 | 대표 프레임워크 | 문서 내 역할 |
|------|------------------|--------------|
| 시장 미시구조 | Kyle 1985, Glosten-Milgrom 1985 | 정보 비대칭과 주문흐름 독성 해석 |
| 변동성 모델 | Heston 1993, Dupire local vol | IV surface와 skew 해석 |
| 레짐 전환 | Hamilton 1989 HMM, Markov switching | 시장 상태 분류 |
| 팩터 모델 | Fama-French 3/5 factor, Carhart momentum | 구조적 배경 필터 |
| 옵션/변동성 전략 | Carr-Madan, variance risk premium literature | 옵션 구조 전략 설계 |
| 실행 알고리즘 | Almgren-Chriss 2001, Kissell TCA | 슬리피지와 충격 최소화 |
| 머신러닝 검증 | Lopez de Prado | meta-labeling, purged CV, deflated Sharpe |
| 포지션 관리 | Kelly criterion, fractional Kelly | 베팅 크기 제어 |
| 시장조성/호가동학 | Avellaneda-Stoikov | 미시 유동성 해석 |
| 꼬리위험 | Taleb convexity framework | tail hedge와 손실 비대칭 통제 |

### 2.2 적용 원칙

- 논문 이름을 장식으로 넣지 않는다.
- 채택하는 모든 지표는 입력, 계산, 해석, 실패 조건을 함께 정의한다.
- 한국 옵션시장 현실에 맞지 않는 모델은 원형 유지보다 로컬 적합화가 우선이다.
- 백테스트에서 강해 보여도 실거래 마찰비용과 유동성 제약을 이기지 못하면 폐기한다.

---

## 3. Market Scope and Operating Constraints

### 3.1 대상 시장

- 주시장: KOSPI200 옵션
- 보조 시장: KOSPI200 선물, KOSPI 현물, 주요 대형주 옵션 데이터, USDKRW
- 글로벌 보조 확인 신호: VIX, US 10Y, USDCNH, S&P500 futures, MOVE index

### 3.2 운영 제약

- 기본 시간축: 1분봉 중심
- 보조 시간축: 틱, 3분, 5분, 15분, 일중 누적
- 원칙: 당일 청산 절대 준수
- 장마감 전 강제평탄화: 15:10 이전 완료
- 야간 리스크 노출: 원칙적으로 금지
- 브로커 전제: 키움 OpenAPI+ 기반 실거래/모의 연동

### 3.3 전략 철학의 현실화

미국 대형 옵션시장과 달리 한국 옵션시장은 특정 시간대 유동성 편중, 만기일 왜곡, 수급 주체 집중, 단기 감마 쏠림의 영향이 더 크다. 따라서 MAHDI v4는 장기 옵션 포지셔닝보다 intraday 구조 우위와 당일 리스크 회수를 더 중시한다.

---

## 4. Data Architecture

### 4.1 핵심 데이터와 보조 데이터

| 구분 | 데이터 | 용도 | 모니터 주기 |
|------|--------|------|-------------|
| 핵심 | KOSPI200 선물 가격/거래량 | 기초자산 방향과 속도 | 틱, 1분 |
| 핵심 | 옵션 체인 가격, IV, OI, 거래량 | 옵션 구조 해석 | 틱, 1분 |
| 핵심 | 호가창 잔량, 체결강도, 미결제약정 변화 | 주문흐름 독성 | 틱 |
| 핵심 | 외국인/기관/개인 수급 | 주체 우위 판단 | 1분, 누적 |
| 핵심 | ATM 중심 Greeks, GEX, gamma flip | 감마 지형 파악 | 1분 |
| 핵심 | realized volatility, range expansion | 변동성 상태 | 1분, 5분 |
| 핵심 | VPIN, order imbalance, microprice | 독성 주문 측정 | 틱, 1분 |
| 핵심 | VWAP, anchored VWAP, volume profile | 공정가치 및 체결 밀집 구간 | 1분 |
| 보조 | USDKRW | 외국인 흐름 선행 확인 | 1분, 5분 |
| 보조 | VIX, VIX term structure | 글로벌 리스크오프 필터 | 장전, 5분 |
| 보조 | USDCNH, US 10Y, MOVE | 매크로 리스크 확인 | 장전, 5분 |
| 보조 | 뉴스/공시/이벤트 캘린더 | 이벤트 리스크 필터 | 장전, 이벤트 발생 시 |
| 보조 | 만기 캘린더, MSCI/지수 리밸런싱 일정 | 흐름 왜곡 사전 경보 | 장전 |

### 4.2 데이터 품질 원칙

- 틱 누락, 체결 지연, 비정상 스파이크를 별도 플래그로 관리한다.
- 실시간 신호와 백테스트 신호는 동일한 feature definition을 써야 한다.
- 누적 수급 데이터는 세션 중 재집계가 가능해야 한다.
- 옵션 체인은 행사가별 결측과 스프레드 확대 구간을 명시적으로 처리한다.

### 4.3 Feature Store 계층

```text
Raw Feed Layer
  -> Cleaned Market Feed Layer
  -> Feature Layer
  -> Signal Layer
  -> Decision Layer
  -> Execution Log Layer
  -> Research Archive Layer
```

---

## 5. Architecture Overview

```text
┌────────────────────────────────────────────────────────────────────┐
│                         MAHDI COMMAND CENTER                       │
├────────────────────────────────────────────────────────────────────┤
│  PART A. Regime + Macro Filter                                    │
│  PART B. Order Flow + Microstructure                              │
│  PART C. Options Surface + Gamma Map                              │
│  PART D. Signal Fusion + Meta Label                               │
│  PART E. Risk Budget + Portfolio Greeks                           │
│  PART F. Execution + Exit Orchestrator                            │
│  PART G. Learning + Validation + Monitoring                       │
└────────────────────────────────────────────────────────────────────┘
```

시스템은 4개의 판단 계층으로 작동한다.

1. 관측: 시장에서 무엇이 일어나고 있는가
2. 해석: 그 변화는 어떤 구조를 의미하는가
3. 결정: 지금 어떤 거래를, 얼마나, 어떤 리스크 한도 안에서 해야 하는가
4. 실행: 어떻게 진입하고, 어떻게 축소하고, 언제 철수할 것인가

---

## 6. Core Engine 1: Regime Intelligence Engine

### 6.1 목적

개별 지표의 좋고 나쁨보다 지금 시장이 어떤 상태에 있는지를 먼저 판단한다. 레짐 엔진은 모든 하위 엔진의 가중치 스위치다.

### 6.2 상태 공간

| Regime | 의미 | 우선 전략 |
|--------|------|-----------|
| TREND_UP_STRONG | 상승 추세 강함 | 콜 매수, 콜 스프레드, pullback 매수 |
| TREND_DOWN_STRONG | 하락 추세 강함 | 풋 매수, 풋 스프레드 |
| RANGE_BALANCED | 평균회귀 우세 | 단기 mean reversion, premium decay 활용 |
| RANGE_BREAK_PREP | 압축 후 확장 대기 | breakout 준비 |
| VOL_EXPANSION | 변동성 팽창 | directional long gamma 우세 |
| VOL_COMPRESSION | 변동성 압축 | 돌파 대기, 비대칭 진입 준비 |
| LIQUIDITY_THIN | 유동성 빈약 | 규모 축소, 선택적 거래 |
| CRISIS_DEFENSE | 리스크 이벤트 | 신규 진입 제한, tail hedge 우선 |

### 6.3 입력 변수

- Hurst exponent
- ADX, realized volatility ratio
- intraday breadth
- cross-asset stress proxies
- order book thinning
- VIX term structure
- 옵션 ATM IV 변화율

### 6.4 의사결정 규칙

- 레짐은 단일 지표가 아니라 베이지안 점수로 산출한다.
- 1분 기반 단기 레짐과 15분 기반 상위 레짐이 충돌하면 상위 레짐에 우선권을 준다.
- CRISIS_DEFENSE로 전환되면 신규 방향 베팅은 차단하거나 최소 사이즈만 허용한다.

### 6.5 실패 조건

- 이벤트 직후 분산 급등으로 레짐 확률이 불안정한 경우
- 장 초반 10분처럼 데이터 축적이 부족한 구간
- 만기일 특정 구간의 왜곡으로 평균적 패턴이 무의미해지는 경우

---

## 7. Core Engine 2: Order Flow and Microstructure Engine

### 7.1 목적

캔들보다 먼저 체결과 호가의 비대칭을 읽는다. 이 엔진은 가격 움직임의 원인을 해석한다.

### 7.2 핵심 지표

| 지표 | 의미 | 해석 |
|------|------|------|
| VPIN | informed trading probability | 급등 시 방향성 추종 또는 방어 강화 |
| Order imbalance | 매수/매도 체결 비대칭 | 추세 지속력 확인 |
| Microprice | 호가 기반 중심가격 | 단기 선행가격 |
| Queue imbalance | 호가잔량 불균형 | 체결 압력 방향 |
| Absorption | 대량 소화 여부 | 반전 또는 지속의 핵심 단서 |
| Sweeps / aggressive prints | 공격적 체결 | 스마트머니 흔적 탐지 |

### 7.3 해석 로직

- 가격 상승과 동시에 order imbalance, microprice, aggressive buy sweep이 동조하면 진성 추세로 가중한다.
- 가격 상승인데 흡수 매도가 누적되면 추세 약화 또는 false breakout 가능성을 높인다.
- VPIN 급등 구간에서는 mean reversion보다 추세 추종 또는 거래회피가 우세하다.

### 7.4 헤지펀드식 확장

- LOB event tagging: 대기 잔량 취소, 신규 적층, sweep 체결을 이벤트 레벨로 태깅
- toxicity zones: 특정 시간대와 가격대의 독성 지대 학습
- maker/taker stress meter: 수동 유동성 공급자와 공격 체결자 우위를 시각화

---

## 8. Core Engine 3: Options Intelligence Engine

### 8.1 목적

옵션시장은 방향성만 반영하지 않는다. 변동성 기대, 꼬리위험 가격, 딜러 감마 포지션, 수급 비대칭이 모두 녹아 있다.

### 8.2 핵심 구성

| 항목 | 기능 |
|------|------|
| IV surface | 행사가, 만기별 변동성 지도 |
| Skew / smile | downside fear, upside squeeze 해석 |
| GEX | 감마 지형과 시장 안정/불안정 추정 |
| Gamma flip | 딜러 헤지 방식 전환 구간 탐지 |
| OI migration | 스마트머니 집중 이동 파악 |
| IV-RV spread | 변동성 프리미엄 여부 판단 |
| Put-Call skew pressure | 꼬리위험 수요 해석 |

### 8.3 핵심 질문

- 지금 옵션 시장은 미래 변동성을 과대평가하고 있는가, 과소평가하고 있는가
- 딜러는 시장을 안정시키는 쪽에 있는가, 불안정하게 만드는 쪽에 있는가
- 특정 행사가에 감마 벽이 형성되어 가격이 붙들릴 가능성이 있는가
- 오늘의 수급은 방향성 베팅인가, 보호 목적 헤지인가

### 8.4 전략 연결

- positive GEX 환경에서는 mean reversion 성격의 짧은 익절 전략 가중
- negative GEX와 gamma flip 하방 이탈 시 directional long gamma 전략 가중
- IV가 RV 대비 과도하게 높고 레짐이 안정적이면 premium selling 계열을 제한적으로 고려
- 이벤트 직전 IV 급등과 skew 왜곡은 비선형 리스크 신호로 취급

---

## 9. Core Engine 4: Volume Structure and Fair Value Engine

### 9.1 목적

시장의 공정가치와 분쟁 지대를 정의한다. 어디에서 가장 많이 싸웠는가를 모르면 진입과 청산의 질이 떨어진다.

### 9.2 핵심 요소

- Session VWAP
- Anchored VWAP from open, high-volume event, breakout point
- Volume profile: POC, VAH, VAL
- HVN/LVN structure
- Volume spike and exhaustion

### 9.3 해석 원칙

- 가격이 VWAP 위, 외국인 수급 우위, 감마 구조 우호, 미시체결 강세가 동시에 나오면 추세 확률을 높인다.
- LVN 돌파는 속도 구간이며, HVN 복귀는 회귀 가능성을 높인다.
- POC 붕괴는 단순 가격 이탈이 아니라 균형점 상실로 해석한다.

---

## 10. Core Engine 5: Signal Fusion and Meta-Decision Engine

### 10.1 목적

좋은 지표를 많이 모으는 것이 아니라, 서로 다른 지표가 같은 가설을 지지하는지 판정한다.

### 10.2 구조

```text
Primary Signal Layer
  -> Regime Weighting Layer
  -> Conflict Resolution Layer
  -> Meta Label Classifier
  -> Conviction Score
  -> Trade Permission
```

### 10.3 1차 시그널

- 방향 시그널
- 변동성 시그널
- 미시구조 시그널
- 수급 시그널
- 감마 구조 시그널

### 10.4 Meta-Labeling

Lopez de Prado 방식의 2단계 구조를 채택한다.

- 1단계: 진입 후보 생성
- 2단계: 그 진입을 실제로 실행할 가치가 있는지 필터링

Meta model 입력 예시:

- regime confidence
- signal agreement count
- recent slippage state
- gamma regime
- foreign flow alignment
- news/event proximity

### 10.5 최종 출력

- NO TRADE
- SMALL TEST SIZE
- STANDARD SIZE
- HIGH CONVICTION SIZE

---

## 11. Core Engine 6: Strategy Palette Engine

### 11.1 방향성 전략

| 전략 | 사용 조건 | 비고 |
|------|-----------|------|
| ATM call buy | 상승 추세 + 독성 매수 + 음의 감마 | 속도장 대응 |
| ATM put buy | 하락 추세 + 방어 수급 약화 | 급락 대응 |
| call debit spread | 상승은 보되 IV 부담 큼 | 비용 절감 |
| put debit spread | 하락 보되 premium 부담 큼 | 손실 상한 명확 |

### 11.2 변동성 전략

| 전략 | 사용 조건 | 비고 |
|------|-----------|------|
| long straddle | 이벤트 전후 변동성 폭발 기대 | 감마 우위 |
| long strangle | 폭발 예상이나 비용 민감 | 꼬리 노림 |
| short premium tactical | IV 과대 + positive GEX + 구조 안정 | 매우 제한적 운영 |
| calendar bias trade | 근월 과열/원월 상대 저평가 | term structure 활용 |

### 11.3 구조적 전략 선택 규칙

- MAHDI는 무조건 많은 전략을 돌리지 않는다.
- 하루 레짐당 우선 전략군을 2개 이하로 제한한다.
- 변동성 매도 전략은 시스템 최고 신뢰 레벨에서만 허용한다.

---

## 12. Advanced Alpha Engine 1: Statistical Arbitrage and Relative Value

### 12.1 목적

절대 방향성 예측이 애매할 때도 상대가치 왜곡에서 기회를 찾는다.

### 12.2 적용 영역

- call-put parity deviation monitoring
- synthetic future vs actual future discrepancy
- cross-expiry IV distortion
- same-delta skew mispricing
- sector-linked index hedge mismatch

### 12.3 대표 기법

- cointegration-based spread monitoring
- z-score reversion on option implied spread
- intraday basis dislocation capture
- dispersion proxy between index option IV and major constituents

### 12.4 주의사항

- 한국 옵션시장에서는 거래비용과 체결 리스크가 상대가치 알파를 상당히 깎는다.
- 따라서 상대가치 전략은 완전 자동보다는 경보형 또는 반자동형이 현실적이다.

---

## 13. Advanced Alpha Engine 2: Factor and Behavioral Overlay

### 13.1 목적

단기 옵션 매매도 결국 더 큰 자금의 포지셔닝과 심리 구조 위에서 발생한다. 팩터와 행동 편향은 단기 신호의 배경음악이다.

### 13.2 팩터 필터

- market beta regime
- size tilt risk
- value vs growth rotation
- profitability quality filter
- momentum continuation probability

### 13.3 행동재무 필터

- opening overreaction reversal
- lunch-hour liquidity vacuum
- expiry-week gamma squeeze
- retail chase behavior after breakout candles
- loss-aversion driven panic hedging

### 13.4 적용 방식

- 팩터와 행동지표는 단독 진입 신호가 아니다.
- 코어 시그널의 강도와 유지시간을 조정하는 overlay로 쓴다.

---

## 14. Advanced Alpha Engine 3: Tail Risk and Convexity Engine

### 14.1 목적

시스템이 가장 위험한 순간은 틀렸을 때가 아니라, 틀린 방향으로 시장이 빠르게 가속될 때다. 이 엔진은 손실의 오른쪽 꼬리가 아니라 왼쪽 꼬리를 끊기 위해 존재한다.

### 14.2 기능

- intraday crash detector
- correlation spike alarm
- order book vacuum alarm
- forced deleveraging protocol
- emergency hedge routing

### 14.3 방어 시나리오

| 상황 | 조치 |
|------|------|
| 2 sigma adverse move | 신규 진입 중단, 포지션 절반 축소 검토 |
| 3 sigma adverse move | 강제 리스크 회수, 손실확대 전략 금지 |
| gamma wall 붕괴 + 외국인 역행 | 전량 축소 또는 완전 철수 |
| 유동성 공백 + 스프레드 급확대 | 시장가 추격 금지, 손절 방식 전환 |

### 14.4 핵심 철학

수익은 공격에서 나오지만, AUM은 방어에서 살아남는다.

---

## 15. Core Engine 7: Portfolio Risk and Capital Allocation Engine

### 15.1 목적

좋은 진입도 사이즈가 틀리면 나쁜 거래가 된다. 이 엔진은 포지션 단위가 아니라 포트폴리오 단위로 위험을 본다.

### 15.2 핵심 관리 항목

- trade-level loss cap
- daily loss cap
- regime-adjusted size cap
- portfolio delta, gamma, vega aggregation
- exposure by expiry and strike cluster
- correlation-adjusted concentration
- liquidity-adjusted position size

### 15.3 사이징 프레임워크

```text
Base Size
  x Regime Confidence
  x Signal Quality
  x Liquidity Score
  x Drawdown Adjustment
  x Portfolio Capacity Constraint
  = Final Tradable Size
```

### 15.4 Kelly 사용 원칙

- full Kelly 금지
- half Kelly 또는 quarter Kelly 기본
- drawdown 구간에서는 Kelly weight를 자동 축소
- high-conviction라고 해도 상한선을 절대 넘지 않는다

### 15.5 회복 프로토콜

- 하루 손실 한도 초과 시 신규 거래 중단
- 3일 연속 기준 손실 초과 시 시스템 review 모드 전환
- 회복 국면에서는 승률보다 손실 축소와 실행 품질 회복을 우선한다

---

## 16. Core Engine 8: Execution and Exit Orchestrator

### 16.1 목적

알파가 좋아도 체결이 나쁘면 시스템은 무너진다. 실행 엔진은 슬리피지를 줄이고, 청산 엔진은 가설이 틀렸을 때 시간을 낭비하지 않는다.

### 16.2 진입 규율

- 시그널 발생 즉시 시장가 추격을 기본값으로 두지 않는다.
- 호가 간격, 체결 빈도, 잔량 두께를 고려해 공격성 수준을 조절한다.
- opening 5분과 특정 이벤트 직후에는 execution aggressiveness를 자동 하향한다.

### 16.3 청산 레이어

| 레이어 | 의미 |
|--------|------|
| Hard stop | 절대 허용 손실 한도 |
| Structure stop | VWAP, POC, gamma wall 이탈 |
| Flow stop | 외국인 수급 반전, microprice 역행 |
| Belief decay stop | meta probability 하락 |
| Time stop | 기대했던 속도가 나오지 않음 |
| Forced flat | 15:10 이전 무조건 청산 |

### 16.4 확률 기반 청산

진입 후에도 매 1분마다 기대값을 다시 계산한다.

- EV 감소
- regime deterioration
- volatility state mismatch
- slippage deterioration

이 네 항목이 동시에 악화되면 손익과 무관하게 축소 또는 철수한다.

### 16.5 실행 알고리즘

- passive-first limit entry in stable regime
- urgency mode in negative gamma expansion
- partial scaling out around gamma walls
- no-averaging-down default rule
- partial fill aware order state machine

---

## 17. Core Engine 9: Self-Learning and Research Intake Engine

### 17.1 목적

시스템은 고정된 룰북이 아니라 학습하는 연구 조직이어야 한다.

### 17.2 연구 생명주기

```text
Idea Intake
  -> Data Specification
  -> Offline Test
  -> Robustness Test
  -> Shadow Deployment
  -> Small Capital Trial
  -> Promotion / Rejection
```

### 17.3 학습 항목

- feature importance drift
- regime-specific hit ratio
- slippage trend
- time-of-day edge decay
- expiry-week anomaly persistence

### 17.4 운영 원칙

- 챔피언 모델과 도전자 모델을 분리한다.
- 실거래 모델은 안정성을 우선한다.
- 새 아이디어는 shadow 모드 없이 실전 배치하지 않는다.

---

## 18. Core Engine 10: Validation and Backtest Standards

### 18.1 검증 철학

강한 백테스트는 출발점일 뿐이다. 실전 배치 기준은 강한 백테스트가 아니라, 마찰비용과 드리프트를 이겨낸 뒤에도 남는 기대값이다.

### 18.2 필수 검증 스택

- walk-forward optimization
- purged k-fold cross validation
- combinatorial purged CV where needed
- Monte Carlo path reshuffling
- stress testing under spread widening
- regime segmentation test
- post-cost attribution
- deflated Sharpe ratio

### 18.3 반드시 체크할 질문

- 수익이 특정 몇 일에만 집중되는가
- 만기일 효과를 빼면 알파가 남는가
- 슬리피지 2배 가정에서도 생존하는가
- 장초반, 점심, 장후반 어느 구간에 편향되는가
- 손익의 대부분이 한 전략 또는 한 레짐에서만 나오는가

### 18.4 폐기 기준

- out-of-sample 약화가 과도할 때
- 비용 반영 후 기대값이 불안정할 때
- 해석 가능한 원인이 없는 경우
- regime transferability가 약할 때

---

## 19. Intraday Trading Playbook

### 19.1 장전 루틴

- 글로벌 리스크 상태 점검
- 당일 이벤트, 경제지표, 옵션 만기 캘린더 확인
- 전일 주요 gamma wall, high OI strike, POC 확인
- USDKRW, 야간 선물, 미국 변동성 체크
- 오늘의 우선 시나리오 3개 작성

### 19.2 장초반 루틴

- opening auction 이후 5분은 관찰 비중 확대
- 외국인/기관 초기 수급 방향 체크
- VWAP 형성 위치 확인
- 초기 급등락이 구조적 추세인지 noise인지 분류

### 19.3 장중 루틴

- 매 1분 레짐 업데이트
- gamma map 및 OI migration 체크
- 포지션별 Greeks, 손익, belief score 재평가
- 손익보다 구조 훼손 여부를 먼저 본다

### 19.4 장마감 루틴

- 강제평탄화 완료 확인
- 거래 로그, 체결 품질, 이유 코드 저장
- 예상과 실제 차이 기록
- 내일 연구 큐에 넣을 관찰점 태깅

---

## 20. Monitoring and Alert System

### 20.1 실시간 경보 체계

| 경보 | 트리거 | 액션 |
|------|--------|------|
| Regime flip alert | 추세에서 위기 또는 압축으로 급변 | 전략군 전환 |
| Toxic flow alert | VPIN, imbalance 급등 | 진입 축소 또는 회피 |
| Gamma breach alert | 핵심 감마 레벨 돌파 | 청산/확대 판단 |
| Slippage alert | 체결비용 급상승 | 실행 알고리즘 방어모드 |
| Drawdown alert | 일중 손실 한도 접근 | 신규 거래 제한 |
| Data quality alert | 체결 누락, feed 지연 | 자동 매매 일시 중지 |

### 20.2 모델 건강도 모니터링

- recent hit rate by regime
- live feature drift score
- expected vs realized slippage
- signal decay by time bucket
- strategy utilization and rejection rate

---

## 21. Dashboard Design

### 21.1 메인 화면 구성

```text
┌──────────────────────────────────────────────────────────────┐
│ MAHDI COMMAND CENTER                                        │
├──────────────────────────────────────────────────────────────┤
│ Regime   | Flow Toxicity | Gamma Map | IV Surface Snapshot  │
│ Position | Portfolio Greeks | PnL | Risk Budget            │
│ Entry Queue | Exit State | Alerts | System Health          │
├──────────────────────────────────────────────────────────────┤
│ Scenario Board: Base / Stress / Crisis                      │
│ Research Notes: Today's anomalies and tags                  │
└──────────────────────────────────────────────────────────────┘
```

### 21.2 핵심 패널 설명

- Regime panel: 현재 레짐, 확률, 상위 레짐과의 정합성
- Flow panel: VPIN, imbalance, absorption, aggressive prints
- Options panel: ATM IV, skew, GEX, gamma flip, key strikes
- Risk panel: 포트폴리오 Greeks, 잔여 일중 손실 버퍼, 강제청산 거리
- Execution panel: 대기 주문, 체결 속도, 미체결 상태, 슬리피지
- Alert panel: 즉시 행동이 필요한 경보만 표시

### 21.3 시각화 원칙

- 예쁜 그래프보다 행동 유도형 레이아웃
- 적색은 위험, 황색은 경고, 청색은 관찰, 녹색은 실행 허용
- 한눈에 지금 해야 할 일과 하면 안 되는 일이 보이게 설계

---

## 22. Database and Logging Blueprint

### 22.1 주요 테이블

| 테이블 | 내용 |
|--------|------|
| market_raw_1m | 선물/옵션/보조시장 원시 1분 데이터 |
| option_chain_ticks | 행사가별 틱 및 호가 데이터 |
| flow_features | VPIN, imbalance, microprice 등 |
| regime_state | 시점별 레짐 확률과 상태 |
| signal_decisions | 진입/보류/거절 결정 로그 |
| execution_logs | 주문, 체결, 슬리피지, 취소 상태 |
| risk_snapshots | Greeks, 손실 버퍼, 노출 상태 |
| trade_history | 거래 단위 결과 |
| research_tags | 이상 현상, 관찰 메모, 아이디어 |

### 22.2 로그 원칙

- 모든 거래는 이유 코드와 함께 저장한다.
- 로그는 손익보다 의사결정 검증용이다.
- 실거래와 백테스트 로그 포맷은 최대한 통일한다.

---

## 23. Infrastructure and Deployment Principles

### 23.1 기술 스택 방향

- Python: 신호 생성, 연구, 브로커 연동 오케스트레이션
- Rust or C++ optional: 고빈도 계산 병목 구간 최적화
- Timeseries DB: 시계열 저장과 질의
- Message queue: 실시간 신호 파이프라인 분리
- Dashboard layer: 경보와 관제 중심 UI

### 23.2 운영 모드

- Research mode
- Paper trading mode
- Shadow live mode
- Reduced capital live mode
- Standard live mode

### 23.3 배포 규율

- 연구 서버와 실거래 서버 분리
- 비상 정지 스위치 필수
- 데이터 끊김 시 자동 리스크 축소
- 환경 설정, 모델 버전, 데이터 버전을 함께 기록

---

## 24. Governance, Compliance, and Risk Discipline

### 24.1 기본 규율

- 계좌 전체 기준 손실 한도 준수
- 하루 최대 거래 횟수 제한
- 동일 가설 반복 진입 제한
- 만기일 특수 규칙 별도 적용
- 시스템 경보 무시 금지

### 24.2 실전 운영에서 금지할 것

- 손실 확대 평균단가 낮추기
- 시장가 추격 중독
- 뉴스 확인 없는 이벤트 진입
- 검증 안 된 새 지표의 즉시 실전 투입
- 장마감 직전 희망성 홀딩

---

## 25. Sample Decision Framework

### 25.1 진입 허용 체크리스트

```text
[ ] 상위 레짐과 하위 레짐이 크게 충돌하지 않는가
[ ] 주문흐름이 가격을 지지하는가
[ ] 옵션 구조가 방향성 또는 변동성 가설을 지지하는가
[ ] 주요 감마 레벨이 현재 포지션에 치명적으로 불리하지 않은가
[ ] 일중 손실 버퍼가 충분한가
[ ] 장마감까지 남은 시간이 전략에 충분한가
[ ] 이벤트 리스크가 통제 가능한가
```

### 25.2 청산 우선 체크리스트

```text
[ ] 최초 진입 가설이 여전히 유효한가
[ ] 구조가 깨졌는가, 아니면 단순 소음인가
[ ] 기대값이 감소했는가
[ ] 슬리피지가 악화되어 남은 알파를 잠식하는가
[ ] 시간 프레임상 더 기다릴 이유가 있는가
```

---

## 26. What Makes This a Top-Tier Options Blueprint

### 26.1 차별점

- 방향 예측 시스템에 머물지 않고 변동성 구조와 감마 구조를 함께 본다.
- 진입보다 청산과 포지션 축소 규칙이 더 정교하다.
- 단일 시그널 만능주의 대신 레짐 기반 가중치 체계를 채택한다.
- 실전 운용에서 가장 중요한 마찰비용, 유동성, 당일청산 제약이 설계 중심에 있다.
- 연구, 배포, 폐기 기준까지 포함한 운용 프레임워크다.

### 26.2 상위 1% 시스템의 조건

상위권 트레이더를 만드는 것은 특별한 지표 하나가 아니다. 다음 다섯 가지가 동시에 있어야 한다.

1. 손실을 제한하는 규율
2. 시장 상태를 읽는 레짐 감각
3. 옵션 구조를 읽는 비선형 사고
4. 실행 품질을 통제하는 미시구조 이해
5. 전략을 버릴 줄 아는 연구 문화

---

## 27. Final Operating Doctrine

```text
Read structure first.
Trade only when structure, flow, and option intelligence align.
Size smaller than your ego wants.
Exit earlier than your hope wants.
Flatten before the market closes.
Review every trade as a researcher, not as a gambler.
```

MAHDI v4의 본질은 화려한 예측 엔진이 아니다.

그 본질은 다음 한 줄로 요약된다.

시장을 맞히려는 시스템이 아니라, 구조적으로 유리할 때만 위험을 꺼내 쓰는 시스템.

---

## 28. Next Build Priorities

1. 데이터 스키마와 피처 사전 확정
2. 레짐 엔진과 옵션 체인 수집기 분리 구현
3. gamma map, VWAP, flow toxicity 대시보드 프로토타입 제작
4. 신호 결정 로그 포맷 통일
5. walk-forward 검증 템플릿 구축
6. 키움 주문 상태머신과 강제청산 모듈 설계

---

## Appendix A. Recommended Signal Hierarchy

| 우선순위 | 신호군 | 이유 |
|----------|--------|------|
| 1 | Regime | 모든 전략의 전제 |
| 2 | Risk | 틀려도 살아남는 구조 확보 |
| 3 | Flow toxicity | 가격의 원인 해석 |
| 4 | Options structure | 감마/IV 기반 비선형 해석 |
| 5 | Volume structure | 공정가치와 이탈 구간 정의 |
| 6 | Meta filter | 실행 가치 최종 판단 |

## Appendix B. Same-Day Closure Rule

당일 청산 원칙은 이 시스템의 운영 헌법이다.

- overnight gap risk를 차단한다.
- 실수와 확신을 구분하게 만든다.
- intraday edge만 검증하게 만든다.
- 자본 회전율과 피드백 속도를 높인다.

장기적으로 강한 시스템은 많이 버는 시스템보다 먼저, 오래 버티는 시스템이다.