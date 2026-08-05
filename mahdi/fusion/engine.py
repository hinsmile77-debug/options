"""Signal Fusion — 파사드 (v6 §11 전체 파이프라인).

Primary Signal Layer -> Ensemble -> Conflict Resolution -> Meta-Label -> Strategy
Palette를 하나의 evaluate() 호출로 통과시킨다. `RiskEngine`과 같은 파사드 패턴 —
이 클래스 자체는 최종 주문 승인권이 없다(그건 RiskEngine의 몫, v6 §12 "독립
거부권"). `FusionDecision`은 Execution Engine이 RiskEngine과 함께 검토할 "진입
후보"일 뿐이다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from mahdi.config.settings import get_strategy_params
from mahdi.engines.regime import RegimeLabel
from mahdi.fusion.conflict_resolution import resolve_conflicts
from mahdi.fusion.ensemble import weighted_consensus
from mahdi.fusion.meta_label import MetaLabelInputs, TradePermission, classify
from mahdi.fusion.signal_layer import MEMBER_FIELDS, MemberScores, SignalInputs, build_member_scores
from mahdi.fusion.strategy_palette import enforce_daily_strategy_cap, select_strategies

# 2026-08-03(운영점검보고서 §5-2) — 이 모듈에는 로그가 하나도 없었다.
#
# 08-03 하루 전체에서 로그를 낸 것은 httpx / mahdi.main / mahdi.broker.rest_client 셋뿐이고,
# fusion·engines·risk·features·execution·learning은 전부 무음이었다. §2-1의 버그(감마플립이
# 전 이력에서 한 번도 산출된 적 없음 → options_flow 멤버 영구 미가용)가 넉 달간 안 보인 이유가
# 정확히 이것이다 — **판단 축에는 관측이 없었다.**
#
# 다만 볼륨을 다시 늘리면 07-31에 어렵게 되찾은 가독성을 또 잃는다(08-03 §2-8: 사람이 읽는 줄이
# 4,629줄로 두 배가 됐다). 그래서 **전이(transition)에만 반응한다** — 매 분 찍지 않고, 멤버
# 가용 조합이나 판정 형태가 **바뀔 때만** 한 줄. 정상적인 하루라면 수 건에 그친다.
# (2026-07-19 §5-5의 WarningThrottle, 07-30 Fix#4의 "상태 전이가 있을 때만 기록"과 같은 계열.)
logger = logging.getLogger("mahdi.fusion.engine")


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
    # 2026-08-05(고도화#4) — **앙상블에 들어간 멤버별 점수 그 자체.**
    #
    # 여기까지 계산해 놓고 버리고 있었다. 그래서 08-05에 판단이 살아났을 때
    # (가용 멤버 2 → 4, 확신도 4종, 전이 83회) **그 방향 ±0.692가 어느 멤버에서 왔는지를
    # DB로 역산할 수 없었다.** `available_member_count` 숫자 하나로는 "몇 개가 살아 있었나"만
    # 알 뿐 "무엇이 판단을 밀었나"에 답하지 못한다.
    #
    # 구체적으로 이 값이 없어서 못 답하는 질문: `OPTIONS_FLOW_GAMMA_WALL_FALLBACK`을 유지할
    # 것인가(08-04 §7 #1, 사용자 결정 대기). 감마월 폴백이 만든 점수가 방향을 뒤집었는지
    # 아니면 다른 멤버에 묻혔는지가 갈리는데, 점수가 안 남아 데이터로 답할 수 없다.
    member_scores: MemberScores | None = None


class SignalFusionEngine:
    def __init__(self, strategy_params: dict | None = None) -> None:
        self._params = strategy_params if strategy_params is not None else get_strategy_params()
        # 직전 사이클의 "판단 형태" — 바뀔 때만 로그를 낸다(위 logger 주석 참고).
        self._last_shape: tuple | None = None

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

        decision = FusionDecision(
            direction=ensemble_result.direction,
            conviction_score=meta_result.conviction_score,
            trade_permission=meta_result.trade_permission,
            allowed_strategies=allowed,
            signal_agreement_count=conflict.agreement_count,
            available_member_count=conflict.available_member_count,
            reject_reasons=reject_reasons,
            member_scores=member_scores,
        )
        self._log_shape_transition(decision, member_scores)
        return decision

    def _log_shape_transition(self, decision: FusionDecision, member_scores: MemberScores) -> None:
        """
        입력: 이번 사이클의 판단과 멤버 점수.
        계산: "어떤 멤버가 살아 있고 / 어떤 허가와 사유가 나왔는가"를 형태(shape)로 압축해,
             직전 사이클과 다를 때만 INFO 한 줄을 남긴다.
        해석: 2026-08-03 §5-2. 매 분 찍으면 하루 495줄이 되어 가독성을 잃고, 아무것도 안 찍으면
             **멤버 하나가 조용히 죽어도 로그만으로는 영영 모른다**(08-03에 실제로 그랬다).
             전이만 남기면 정상일에는 수 건, 무언가 바뀐 날에는 그 시각이 정확히 남는다.
        실패 조건: 없음 — 로깅 실패는 판단에 영향을 주지 않는다.
        """
        available = tuple(
            name for name in MEMBER_FIELDS if getattr(member_scores, name) is not None
        )
        shape = (available, decision.trade_permission, tuple(decision.reject_reasons),
                 tuple(decision.allowed_strategies))
        if shape == self._last_shape:
            return
        previous, self._last_shape = self._last_shape, shape
        logger.info(
            "판단 형태 전이: 가용멤버 %s(%d/%d) · %s · 사유 %s · 전략 %s%s",
            list(available), len(available), len(MEMBER_FIELDS),
            decision.trade_permission.value,
            list(decision.reject_reasons) or "없음",
            list(decision.allowed_strategies) or "없음",
            "" if previous is None else f" (직전 가용멤버 {list(previous[0])})",
        )
