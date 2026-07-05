# [Core Design] Project Mahdi: Ultimate Integrated Trading System

버전: 2.0 (Full Integrated Intelligence)
상태: Production Ready Architecture

---

## 1. 시스템 핵심 구조

마흐디는 5개의 핵심 엔진으로 구성된다:

1. Regime Engine (시장 상태 판단)
2. Position Intelligence Engine (주체 포지션 해석)
3. Volume Structure Engine (가격대 거래량 분석)
4. Options Intelligence Engine (IV, GEX, Skew)
5. Execution & Exit Engine (진입 + 청산 최적화)

---

## 2. 핵심 확장 요소 (추가된 설계)

### 2.1 Volume Intelligence (핵심 차별화)

- Volume-at-Price (VAP)
- Volume Spike Detection
- Liquidity Absorption Detection
- LVN / HVN / POC 구조

핵심 로직:

volume_spike = current_volume / avg_volume

---

### 2.2 Position Tracking (주체 분석)

- 외국인 / 기관 / 개인 VWAP
- 포지션 수익 상태 추적
- Trap Detection

핵심:

if foreign_profit and retail_loss:
    trend_continuation

---

### 2.3 Regime Engine

- Trend
- Range
- Volatility Expansion
- Compression

---

### 2.4 Options Intelligence

- IV vs RV
- Skew
- Gamma Exposure (GEX)
- Gamma Flip

---

## 3. 진입 전략

조건 결합:

- 가격 > VWAP
- OI 증가
- Volume Absorption
- Regime = Trend

→ ATM / ITM 진입

---

## 4. 청산 전략 (강화)

### 기본 청산
- 손절: -1~2%
- 익절: +2~4%

### 구조 기반
- VWAP 이탈
- POC 붕괴

### 수급 기반
- 외국인 포지션 반전

### 확률 기반
- EV 감소
- 모델 확률 하락

---

## 5. Adaptive Exit Engine (핵심 추가)

시장 상태에 따라 청산 자동 조정:

Trend:
    익절 확대 + Trailing

Range:
    빠른 청산

Volatility:
    짧은 보유

---

## 6. 자가학습 엔진

- Feature Store 기반
- EV 모델 학습
- Meta Policy
- Drift Detection

---

## 7. Research Intake 시스템

아이디어 생명주기:

수집 → 검증 → 백테스트 → 섀도우 → 실거래 → 채용

---

## 8. 백테스트 엔진

- 이벤트 기반
- 슬리피지 반영
- Walk-forward
- Monte Carlo

---

## 9. UI 대시보드

구성:

- 레짐 상태
- 시그널
- Volume Heatmap
- Gamma Level
- 포지션 상태

---

## 10. 데이터베이스 구조

### market_raw_1m
- timestamp
- price
- vpin
- usdkrw

### option_analysis_1m
- iv
- skew
- gamma

### prediction_logs
- prediction
- confidence
- actual_return

### trade_history
- entry
- exit
- pnl

---

## 11. 핵심 철학

- 시장은 구조다
- 수익은 확률이다
- 전략은 진화한다

---

## 최종 선언

마흐디는 단순한 트레이딩 시스템이 아니다.

"시장을 학습하고 스스로 진화하는 인공지능 트레이더다"
