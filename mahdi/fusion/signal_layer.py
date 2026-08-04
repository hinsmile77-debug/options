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
    # 2026-08-04 Fix#3 — 감마플립이 없을 때 쓰는 대체 기준점(감마 노출이 가장 큰 행사가).
    # `options_intel.gamma_walls()[0][0]`. 감마플립과 달리 체인만 있으면 **항상 산출된다.**
    gamma_wall: float | None = None
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

# 2026-08-04(운영점검보고서 §2-5 / 고도화#2) — **지금 원리적으로 산출 가능한** 멤버.
#
# `xgboost_tabular`/`lstm_temporal`은 학습된 모델이 없어(trade_history 0건) Phase 3까지 항상
# None이다. 나머지 4개는 원재료만 있으면 산출된다 — 그래서 "이론 최대"는 6이 아니라 **4**다.
#
# 이 값을 여기 두는 이유: 08-04까지 `mahdi/ops/db_metrics.py`가 이론 최대를 **3으로 하드코딩**하고
# 주석에 *"orderflow는 파이프라인 미구현이라 이론상 최대는 3"* 이라고 적어뒀는데, 그 전제가
# 사실이 아니었다(`market_raw_1m.ofi`는 08-04에 선물 410분 전부 채워져 있었다 — §2-5).
# 그 결과 `orderflow_ofi_vpin`이 하루 종일 죽어 있다는 사실이 **지표의 분모 안으로 숨었다.**
# 구현 여부를 아는 것은 이 모듈이므로, 세는 쪽이 여기서 가져다 쓰게 한다(고도화#1 규약 A와 같은
# 원칙 — 사실을 아는 쪽이 상수를 소유한다).
UNTRAINED_MEMBER_FIELDS = ("xgboost_tabular", "lstm_temporal")
IMPLEMENTED_MEMBER_FIELDS = tuple(f for f in MEMBER_FIELDS if f not in UNTRAINED_MEMBER_FIELDS)


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


# 2026-08-04(운영점검보고서 §2-3/§2-4, §7 사용자 결정 #1안 (a) / Fix#3) — 감마플립 폴백 게이트.
#
# `_options_flow_score()`의 경로 A는 `gamma_flip`을 **필수**로 요구했다. 그런데 08-04에
# 감마플립이 이 북에서 **구조적으로 산출되지 않는다**는 것이 확정됐다: ATM 지터가 공짜로 만든
# 25행사가(952.5~1012.5, ±3%) 자연실험에서 먼슬리 콜−풋 OI가 25개 중 20개에서 음수였고,
# 그 폭 전체를 탐색 구간으로 준 `find_gamma_flip`도 None을 돌려줬다(§15 광폭 감마플립).
# 즉 **행사가 창을 넓혀도 해결되지 않는다** — 딜러가 전 구간 한 방향이면 flip 자리가 없다.
#
# 그 결과 v6 §11.3 가중치 0.20짜리 멤버(A3 딜러 헤지 플로우 축)가 넉 달째 죽어 있다.
# 대체 기준점으로 **감마 월(gamma_walls()[0])** 을 쓴다. 감마 월은 저장된 감마·OI만 쓰므로
# 체인이 있으면 항상 산출되고(만기 당일에도 나온다), "딜러 노출이 가장 큰 가격 레벨"이라는
# 점에서 flip과 같은 역할(스팟이 그 위냐 아래냐로 재헤지 방향이 갈린다)을 한다.
#
# **이것은 신호 정의 변경이다**(v6 §11.3 해석). 그래서 상수 하나로 껐다 켤 수 있게 둔다 —
# 앙상블 멤버가 2개에서 3~4개로 늘어나는 변화라, 효과가 나쁘면 즉시 되돌릴 수 있어야 한다.
# 되돌리면 08-04 이전 동작과 **완전히 동일**하다.
OPTIONS_FLOW_GAMMA_WALL_FALLBACK = True


def _options_flow_score(inputs: SignalInputs) -> float | None:
    """
    계산: GEX 부호로 기준선 대비 스팟 위치가 회귀(양수 GEX)인지 증폭(음수 GEX)인지 결정한다
         — 양수면 기준선 쪽으로 되돌아가는 방향(역추세), 음수면 멀어지는 방향(추세 지속).
         기준선은 Gamma Flip이고, 그것이 없으면 감마 월로 폴백한다(2026-08-04 Fix#3, 상세 근거는
         `OPTIONS_FLOW_GAMMA_WALL_FALLBACK` 주석).
         14:00 이후에는 Charm 드리프트 방향을 함께 평균낸다(v6 §13.2 "14:00 이후 Charm 드리프트
         방향 우선", charm_active=True일 때만).
    실패 조건: gex/spot이 없거나 기준선(flip·wall 둘 다)이 없으면 그 성분은 건너뛴다.
              Charm까지 포함해 성분이 하나도 없으면 None.
    """
    components: list[float] = []
    reference = inputs.gamma_flip
    if reference is None and OPTIONS_FLOW_GAMMA_WALL_FALLBACK:
        reference = inputs.gamma_wall
    if inputs.gex is not None and reference is not None and inputs.spot is not None:
        distance_sign = _directional_sign(inputs.spot - reference)
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
