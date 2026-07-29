"""Signal Fusion — Meta-Label 프록시 (v6 §11.2).

실제 López de Prado 2단계 메타 모델(RandomForest/XGBoost, Triple Barrier 레이블 +
Purged K-Fold)은 학습 데이터(`trade_history`)가 쌓여야 만들 수 있다 — 이번 증분
시점엔 0건이다. `engines/regime.py`의 `warmup_fallback()`이 HMM 학습 전 결정론적
대체값을 쓰는 것과 같은 원칙으로, 여기서도 §11.2가 명시한 메타 모델 입력 목록
(regime confidence, 신호 동조 개수, 최근 슬리피지 상태, 감마 레짐, 외국인 플로우
정합성, 이벤트 근접도, 최근 동일 셋업 성과)을 그대로 받아 결정론적 점수로
환산한다. Phase 3에서 `trade_history`가 쌓이면 이 함수를 학습된 분류기로 교체한다
— 그 전까지도 "거절 사유 있는 보수적 게이트"로는 온전히 기능한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TradePermission(str, Enum):
    NO_TRADE = "NO_TRADE"
    SMALL_TEST = "SMALL_TEST"
    STANDARD = "STANDARD"
    HIGH_CONVICTION = "HIGH_CONVICTION"


@dataclass(frozen=True, slots=True)
class MetaLabelInputs:
    regime_confidence: float  # 0~1, RegimeState.prob_vector 최댓값
    signal_agreement_count: int  # ConflictResolution.agreement_count
    available_member_count: int  # ConflictResolution.available_member_count
    recent_slippage_elevated: bool = False
    gamma_regime_stable: bool = True  # 예: GEX>=0 & stability_flag(strategy_gates 개념 재사용)
    foreign_flow_aligned: bool = True  # 외국인 수급 방향이 ensemble 합의 방향과 일치하는지
    event_proximity_minutes: float | None = None  # 다음 매크로 이벤트까지 분
    recent_same_setup_win_rate: float | None = None  # 0~1, None=이력 없음(중립)


@dataclass(frozen=True, slots=True)
class MetaLabelResult:
    conviction_score: float  # 0~1
    trade_permission: TradePermission


def classify(inputs: MetaLabelInputs, thresholds: dict) -> MetaLabelResult:
    """
    입력: MetaLabelInputs, thresholds(`strategy_params.yaml`의 `meta_label_thresholds`).
    계산: base = regime_confidence x (signal_agreement_count / available_member_count).
         이후 슬리피지 악화/감마 레짐 불리/외국인 수급 불일치/이벤트 근접 4개 조건을
         곱셈 페널티로 적용하고, 최근 동일 셋업 성과(recent_same_setup_win_rate)가 있으면
         0.5~1.0배로 추가 스케일한다(자기강화 피드백, v6 §11.2 — 이력 없으면 1.0배 중립).
    해석: score를 no_trade_max/small_test_max/standard_max 3개 경계로 4단계
         TradePermission에 매핑한다.
    실패 조건: available_member_count가 0이면 base=0.0(합의 자체가 성립 불가 -> NO_TRADE).
    """
    agreement_ratio = (
        inputs.signal_agreement_count / inputs.available_member_count
        if inputs.available_member_count > 0
        else 0.0
    )
    score = inputs.regime_confidence * agreement_ratio

    if inputs.recent_slippage_elevated:
        score *= thresholds.get("slippage_penalty_factor", 0.7)
    if not inputs.gamma_regime_stable:
        score *= thresholds.get("gamma_regime_penalty_factor", 0.85)
    if not inputs.foreign_flow_aligned:
        score *= thresholds.get("foreign_flow_penalty_factor", 0.8)

    event_penalty_minutes = thresholds.get("event_proximity_penalty_minutes", 15)
    if (
        inputs.event_proximity_minutes is not None
        and inputs.event_proximity_minutes < event_penalty_minutes
    ):
        score *= thresholds.get("event_proximity_penalty_factor", 0.5)

    if inputs.recent_same_setup_win_rate is not None:
        score *= 0.5 + inputs.recent_same_setup_win_rate / 2

    no_trade_max = thresholds.get("no_trade_max", 0.15)
    small_test_max = thresholds.get("small_test_max", 0.35)
    standard_max = thresholds.get("standard_max", 0.65)

    if score < no_trade_max:
        permission = TradePermission.NO_TRADE
    elif score < small_test_max:
        permission = TradePermission.SMALL_TEST
    elif score < standard_max:
        permission = TradePermission.STANDARD
    else:
        permission = TradePermission.HIGH_CONVICTION

    return MetaLabelResult(conviction_score=score, trade_permission=permission)
