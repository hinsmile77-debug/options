"""Signal Fusion — Primary Signal Layer (v6 §11.1, §11.3).

이 레이어는 새 계산을 하지 않는다 — 이미 존재하는 피처 계산기(engines/regime.py,
features/options_intel.py, features/orderflow.py)의 출력을 앙상블 멤버별 방향성
점수(-1~+1)로 정규화하는 것만 책임진다. v6 §11.3 앙상블 6개 멤버 중
`xgboost_tabular`/`lstm_temporal`은 아직 학습된 모델이 없어(trade_history 0건)
이 증분에서는 항상 None을 반환한다 — Phase 3에서 실제 분류기가 생기면 그 자리를
채운다. 나머지 4개 멤버는 regime_pipeline.compute_macro_score_proxy()와 동일한
원칙(방향은 부호만, 크기 자의적 스케일링 금지)으로 부호 기반 점수를 만든다.
"""

from __future__ import annotations

from dataclasses import dataclass

from mahdi.engines.regime import RegimeLabel, RegimeState

_TREND_DIRECTION: dict[RegimeLabel, float] = {
    RegimeLabel.TREND_UP_STRONG: 1.0,
    RegimeLabel.TREND_DOWN_STRONG: -1.0,
}


def _directional_sign(value: float) -> float:
    if value > 0:
        return 1.0
    if value < 0:
        return -1.0
    return 0.0


@dataclass(frozen=True, slots=True)
class SignalInputs:
    """앙상블 멤버 점수를 만드는 데 필요한 원재료 — 전부 이미 있는 계산기의 출력이다."""

    regime_state: RegimeState | None = None
    gex: float | None = None
    gamma_flip: float | None = None
    spot: float | None = None
    total_charm: float | None = None
    charm_active: bool = False
    ofi: float | None = None
    queue_imbalance: float | None = None
    foreign_net_flow: float | None = None  # 외국인 순매수(원화 등), 부호=방향


@dataclass(frozen=True, slots=True)
class MemberScores:
    """v6 §11.3 앙상블 6개 멤버 각각의 방향성 점수(-1~+1) 또는 None(신호 없음/미학습)."""

    regime_hmm: float | None = None
    xgboost_tabular: float | None = None  # 항상 None — Phase 3 학습 전
    lstm_temporal: float | None = None  # 항상 None — Phase 3 학습 전
    options_flow: float | None = None
    orderflow_ofi_vpin: float | None = None
    flow_position: float | None = None


# ensemble.py/conflict_resolution.py가 공유하는 필드 순회 순서 — strategy_params.yaml의
# ensemble 섹션 키와 1:1 대응.
MEMBER_FIELDS = (
    "regime_hmm",
    "xgboost_tabular",
    "lstm_temporal",
    "options_flow",
    "orderflow_ofi_vpin",
    "flow_position",
)


def _regime_hmm_score(regime_state: RegimeState | None) -> float | None:
    """
    계산: TREND_UP/DOWN_STRONG만 방향성이 있다(RegimeLabel 나머지는 방향 무의미 — v6 §7 참고).
         stability_flag=False(REGIME_UNSTABLE)면 신뢰도를 절반으로 낮춘다.
    실패 조건: regime_state가 None이면 None.
    """
    if regime_state is None:
        return None
    base = _TREND_DIRECTION.get(regime_state.regime, 0.0)
    if base == 0.0:
        return 0.0
    return base if regime_state.stability_flag else base * 0.5


def _options_flow_score(inputs: SignalInputs) -> float | None:
    """
    계산: GEX 부호로 Gamma Flip 대비 스팟 위치가 회귀(양수 GEX)인지 증폭(음수 GEX)인지 결정한다
         — 양수면 flip 쪽으로 되돌아가는 방향(역추세), 음수면 flip에서 멀어지는 방향(추세 지속).
         14:00 이후에는 Charm 드리프트 방향을 함께 평균낸다(v6 §13.2 "14:00 이후 Charm 드리프트
         방향 우선", charm_active=True일 때만).
    실패 조건: gex/gamma_flip/spot 중 하나라도 없으면 그 성분은 건너뛴다. Charm까지 포함해
              성분이 하나도 없으면 None.
    """
    components: list[float] = []
    if inputs.gex is not None and inputs.gamma_flip is not None and inputs.spot is not None:
        distance_sign = _directional_sign(inputs.spot - inputs.gamma_flip)
        if distance_sign != 0.0:
            components.append(-distance_sign if inputs.gex >= 0 else distance_sign)
    if inputs.charm_active and inputs.total_charm is not None:
        charm_sign = _directional_sign(inputs.total_charm)
        if charm_sign != 0.0:
            components.append(charm_sign)
    if not components:
        return None
    return sum(components) / len(components)


def _orderflow_ofi_vpin_score(inputs: SignalInputs) -> float | None:
    """
    계산: OFI 부호(매수/매도 압력)와 잔량 불균형 부호를 평균한다 — 둘 다 순수 방향 지표라
         임의의 스케일 상수 없이 부호만 쓴다(macro_score_proxy와 동일 원칙).
    실패 조건: 둘 다 없으면 None.
    """
    components: list[float] = []
    if inputs.ofi is not None:
        components.append(_directional_sign(inputs.ofi))
    if inputs.queue_imbalance is not None:
        components.append(_directional_sign(inputs.queue_imbalance))
    if not components:
        return None
    return sum(components) / len(components)


def _flow_position_score(inputs: SignalInputs) -> float | None:
    """계산: 외국인 순매수 부호 그대로. 실패 조건: foreign_net_flow가 없으면 None."""
    if inputs.foreign_net_flow is None:
        return None
    return _directional_sign(inputs.foreign_net_flow)


def build_member_scores(inputs: SignalInputs) -> MemberScores:
    """
    입력: SignalInputs(원재료 전부 선택적).
    계산: 4개 멤버(regime_hmm/options_flow/orderflow_ofi_vpin/flow_position) 점수를 계산하고,
         xgboost_tabular/lstm_temporal은 항상 None(미학습).
    해석: 각 필드는 -1(약세)~+1(강세) 또는 None(그 멤버의 신호 없음) — 상위 ensemble.py가
         None을 분모에서 제외해 가중 평균한다.
    실패 조건: 없음 — 원재료 부재는 개별 멤버의 None으로 표현된다.
    """
    return MemberScores(
        regime_hmm=_regime_hmm_score(inputs.regime_state),
        xgboost_tabular=None,
        lstm_temporal=None,
        options_flow=_options_flow_score(inputs),
        orderflow_ofi_vpin=_orderflow_ofi_vpin_score(inputs),
        flow_position=_flow_position_score(inputs),
    )
