"""Signal Fusion — 파사드 (v6 §11 전체 파이프라인).

Primary Signal Layer -> Ensemble -> Conflict Resolution -> Meta-Label -> Strategy
Palette를 하나의 evaluate() 호출로 통과시킨다. `RiskEngine`과 같은 파사드 패턴 —
이 클래스 자체는 최종 주문 승인권이 없다(그건 RiskEngine의 몫, v6 §12 "독립
거부권"). `FusionDecision`은 Execution Engine이 RiskEngine과 함께 검토할 "진입
후보"일 뿐이다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mahdi.config.settings import get_strategy_params
from mahdi.engines.regime import RegimeLabel
from mahdi.fusion.conflict_resolution import resolve_conflicts
from mahdi.fusion.ensemble import weighted_consensus
from mahdi.fusion.meta_label import MetaLabelInputs, TradePermission, classify
from mahdi.fusion.signal_layer import SignalInputs, build_member_scores
from mahdi.fusion.strategy_palette import enforce_daily_strategy_cap, select_strategies


def _sign(value: float) -> float:
    if value > 0:
        return 1.0
    if value < 0:
        return -1.0
    return 0.0


@dataclass(frozen=True, slots=True)
class MetaLabelContext:
    """SignalInputs/앙상블만으로는 알 수 없는, 호출측이 직접 넘겨야 하는 메타 라벨 입력."""

    recent_slippage_elevated: bool = False
    gamma_regime_stable: bool = True
    event_proximity_minutes: float | None = None
    recent_same_setup_win_rate: float | None = None


@dataclass(frozen=True, slots=True)
class FusionDecision:
    direction: float
    conviction_score: float
    trade_permission: TradePermission
    allowed_strategies: list[str] = field(default_factory=list)
    signal_agreement_count: int = 0
    available_member_count: int = 0
    reject_reasons: list[str] = field(default_factory=list)


class SignalFusionEngine:
    def __init__(self, strategy_params: dict | None = None) -> None:
        self._params = strategy_params if strategy_params is not None else get_strategy_params()

    def evaluate(
        self,
        signal_inputs: SignalInputs,
        meta_context: MetaLabelContext,
        vrp: float = 0.0,
        already_used_strategies_today: frozenset[str] = frozenset(),
    ) -> FusionDecision:
        """
        입력: SignalInputs(원재료), MetaLabelContext(슬리피지/감마레짐/이벤트근접/자기강화
             이력 — 신호 계산기만으로는 알 수 없는 값), VRP, 오늘 이미 쓴 전략 집합.
        계산: (1) 멤버별 점수 -> (2) 가중 합의 방향 -> (3) 충돌 해소(동조/반대 개수) ->
             (4) 메타 라벨(conviction_score, TradePermission) -> (5) 합의가 뚜렷하고
             NO_TRADE가 아닐 때만 Strategy Palette 조회 + 하루 상한 적용.
        해석: reject_reasons가 비어 있어야만 allowed_strategies가 실제로 채워진다 —
             NO_TRADE/충돌/팔레트 게이트 미충족 중 하나라도 있으면 빈 목록.
        실패 조건: 없음 — 원재료 부재는 전부 개별 레이어의 None/중립값으로 흡수된다.
        """
        member_scores = build_member_scores(signal_inputs)
        ensemble_result = weighted_consensus(member_scores, self._params.get("ensemble", {}))
        conflict = resolve_conflicts(member_scores, ensemble_result)

        regime_state = signal_inputs.regime_state
        regime_confidence = max(regime_state.prob_vector) if regime_state is not None else 0.0
        regime = regime_state.regime if regime_state is not None else RegimeLabel.RANGE_BALANCED

        foreign_flow_aligned = True
        consensus_sign = _sign(ensemble_result.direction)
        if signal_inputs.foreign_net_flow is not None and consensus_sign != 0.0:
            foreign_sign = _sign(signal_inputs.foreign_net_flow)
            foreign_flow_aligned = foreign_sign == 0.0 or foreign_sign == consensus_sign

        meta_inputs = MetaLabelInputs(
            regime_confidence=regime_confidence,
            signal_agreement_count=conflict.agreement_count,
            available_member_count=conflict.available_member_count,
            recent_slippage_elevated=meta_context.recent_slippage_elevated,
            gamma_regime_stable=meta_context.gamma_regime_stable,
            foreign_flow_aligned=foreign_flow_aligned,
            event_proximity_minutes=meta_context.event_proximity_minutes,
            recent_same_setup_win_rate=meta_context.recent_same_setup_win_rate,
        )
        meta_result = classify(meta_inputs, self._params.get("meta_label_thresholds", {}))

        reject_reasons: list[str] = []
        allowed: list[str] = []
        if meta_result.trade_permission == TradePermission.NO_TRADE:
            reject_reasons.append("meta_label:no_trade")
        elif not conflict.has_clear_consensus:
            reject_reasons.append("conflict_resolution:no_clear_consensus")
        else:
            palette = select_strategies(
                regime,
                vrp,
                highest_confidence=meta_result.trade_permission == TradePermission.HIGH_CONVICTION,
                positive_gex=signal_inputs.gex is not None and signal_inputs.gex >= 0,
                stable_regime=regime_state.stability_flag if regime_state is not None else False,
            )
            if palette.reason:
                reject_reasons.append(f"strategy_palette:{palette.reason}")
            strategy_gates = self._params.get("strategy_gates", {})
            cap = strategy_gates.get("max_priority_strategies_per_regime_day", 2)
            allowed = enforce_daily_strategy_cap(palette.allowed_strategies, already_used_strategies_today, cap)

        return FusionDecision(
            direction=ensemble_result.direction,
            conviction_score=meta_result.conviction_score,
            trade_permission=meta_result.trade_permission,
            allowed_strategies=allowed,
            signal_agreement_count=conflict.agreement_count,
            available_member_count=conflict.available_member_count,
            reject_reasons=reject_reasons,
        )
