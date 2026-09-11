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

# 기준선의 출처 — `signal_decisions.gamma_reference_source`(마이그레이션 037)에 그대로 실린다.
REFERENCE_SOURCE_FLIP = "flip"
REFERENCE_SOURCE_WALL = "wall"
REFERENCE_SOURCE_NONE = "none"


def options_flow_reference(
    gamma_flip: float | None, gamma_wall: float | None
) -> tuple[float | None, str]:
    """
    입력: 그 분의 감마플립과 감마 월(둘 다 없을 수 있다).
    계산: `_options_flow_score()`가 실제로 쓸 기준선과 **그 출처 이름**을 함께 돌려준다.
    해석: 2026-09-03 — 폴백 규칙이 두 곳에 적히는 것을 막는 자리다. 점수를 내는 쪽
         (`_options_flow_score`)과 그 값을 기록하는 쪽(`main._build_signal_inputs`)이
         **같은 함수**를 부른다. 규칙을 아는 쪽이 소유한다는 `IMPLEMENTED_MEMBER_FIELDS`의
         원칙과 같다 — 기록이 판단을 복사하면 게이트 상수를 껐을 때 둘이 조용히 갈린다.

         `SignalInputs`가 아니라 원시 두 값을 받는 이유: `main._build_signal_inputs()`는
         `SignalInputs`를 만들기 **전에** 기록용 dict를 조립한다. 그 순서를 뒤집는 것보다
         이 함수의 입력을 좁히는 편이 배선이 짧다.
    실패 조건: 없음 — 둘 다 없으면 `(None, "none")`이다. 게이트를 끄면 월이 있어도
              `(None, "none")`이 되고, 이는 08-04 이전 동작과 정확히 같다.
    """
    if gamma_flip is not None:
        return gamma_flip, REFERENCE_SOURCE_FLIP
    if OPTIONS_FLOW_GAMMA_WALL_FALLBACK and gamma_wall is not None:
        return gamma_wall, REFERENCE_SOURCE_WALL
    return None, REFERENCE_SOURCE_NONE


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
    reference, _source = options_flow_reference(inputs.gamma_flip, inputs.gamma_wall)
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


# ===== 2026-09-11 (09-11 §3-1 / 제4부 P1-3) — **이탈 줄이 「왜」를 말하지 않았다** =====
#
# ## 그날 두 축이 조용히 빠졌고, 사유는 아무 데도 없었다
#
# 09-11 15:24:54에 `options_flow`가, 15:36:54에 `orderflow_ofi_vpin`이 가용 목록에서 빠졌다.
# 「판단 축 이탈」 줄(08-23 Fix#3)은 **언제·몇 개**를 정확히 말했지만 — `(가용 4→3, 비영 2→2)
# · 직전 편입 09:00:00 · 384분 유지` — **왜**는 말하지 않는다. 장후 회차가 §3-1에 *"입력
# 데이터 고갈인지 예외 처리 경로인지 로그만으로는 안 갈린다"*고 적고 원인 규명을 다음으로 넘겼다.
#
# ## ⚠ 리포트가 제안한 어휘를 그대로 쓰지 않았다 — 착수 전 코드 확인에서 뒤집혔다
#
# 제4부 P1-3은 사유 값으로 `input_missing` / `exception` / `timeout` 셋을 제안했다.
# 그런데 `build_member_scores()`의 docstring이 이미 못박아 뒀다 — *"실패 조건: 없음 —
# 원재료 부재는 개별 멤버의 None으로 표현된다."* **이 레이어에는 예외를 삼키는 자리도
# 타임아웃도 없다.** 뒤 두 값은 영원히 안 나오는 값이고, 찍으면 거짓말이 된다
# (없는 분기를 가리키는 어휘가 로그에 남으면 다음 사람이 그 경로를 찾느라 시간을 쓴다).
#
# 대신 **실제로 갈리는 두 갈래**를 쓴다. 리포트의 세 값 어디에도 자리가 없던 갈래가 있다:
#
#   ⓐ `원재료없음[...]` — 그 멤버가 읽는 입력 필드가 실제로 비어 있었다. 이름까지 적는다.
#   ⓑ `부호0[...]`       — 입력은 **다 있었는데** 방향 부호가 0이라 성분이 기각됐다.
#                          `_options_flow_score`의 `distance_sign == 0.0`(스팟이 기준선과
#                          정확히 같은 값) 과 `charm_sign == 0.0`이 그 자리다.
#
# ⓑ는 「입력 고갈」과 **전혀 다른 사건**이다 — 데이터는 멀쩡히 흘러들어왔고 그날 시장이
# 마침 기준선 위에 얹혀 있었다는 뜻이다. 이 둘을 한 칸에 섞으면 09-11이 답을 못 찾은 그
# 질문("고갈인가")에 다음에도 답할 수 없다.
#
# ## 규칙을 아는 쪽이 소유한다
#
# 이 함수가 `engine.py`가 아니라 여기 있는 이유는 `IMPLEMENTED_MEMBER_FIELDS`·
# `options_flow_reference()`와 같다 — **점수를 내는 규칙과 「왜 못 냈는가」를 말하는 규칙은
# 같은 규칙**이다. 갈라 두면 `_options_flow_score`의 조건이 바뀐 날 사유만 옛말을 하고,
# 그것은 틀린 로그라 없느니만 못하다. 아래 각 절은 위 점수 함수의 조건을 **그대로** 따라간다
# (`tests/test_fusion_engine_axis_exit_reason.py`가 둘의 일치를 못박는다).
#
# ⛔ **판정은 한 글자도 안 바뀐다.** 이 함수는 점수를 만들지도, 되돌려 쓰지도 않는다 —
# 같은 입력을 한 번 더 읽어 문자열을 만들 뿐이다. 로그 문구에만 쓰인다.
REASON_UNTRAINED = "미학습"
REASON_UNKNOWN = "미상"


def _options_flow_absence(inputs: SignalInputs) -> tuple[list[str], list[str]]:
    """`_options_flow_score()`가 성분을 하나도 못 모은 이유 — (없던 원재료, 부호가 0이던 성분)."""
    missing: list[str] = []
    zero_sign: list[str] = []
    reference, source = options_flow_reference(inputs.gamma_flip, inputs.gamma_wall)
    if inputs.gex is None:
        missing.append("gex")
    if reference is None:
        # 폴백까지 갔는데도 없으면 **둘 다** 없던 것이다(게이트가 꺼져 있으면 월은 애초에 안 본다).
        missing.append("gamma_flip" if not OPTIONS_FLOW_GAMMA_WALL_FALLBACK else "gamma_flip·wall")
    if inputs.spot is None:
        missing.append("spot")
    if not missing and _directional_sign(inputs.spot - reference) == 0.0:
        # 스팟이 기준선과 정확히 같다 — 데이터는 멀쩡했고 방향이 없었을 뿐이다.
        zero_sign.append(f"스팟−기준선({source})")
    # Charm 성분은 **14:00 이후에만** 대상이다(v6 §13.2). 비활성 시간대의 부재는 결손이
    # 아니므로 세지 않는다 — 세면 오전 내내 있지도 않은 결손이 로그에 남는다.
    if inputs.charm_active:
        if inputs.total_charm is None:
            missing.append("total_charm")
        elif _directional_sign(inputs.total_charm) == 0.0:
            zero_sign.append("total_charm")
    return missing, zero_sign


def member_absence_reason(field: str, inputs: SignalInputs) -> str:
    """
    입력: 멤버 필드 이름과 그 사이클의 원재료.
    계산: 그 멤버의 점수가 None인 이유를 **위 점수 함수들의 조건 그대로** 되짚어 한 문장으로
         만든다. 상세 근거는 위 `REASON_UNTRAINED` 절.
    해석: 「판단 축 이탈」 줄 끝에 붙는 꼬리표다. `원재료없음[...]`은 입력이 끊긴 것이고,
         `부호0[...]`은 **입력은 멀쩡했는데 방향이 없던 것**이다 — 조치가 다르다.
    실패 조건: 없음 — 어느 갈래에도 안 걸리면 `미상`이다(규약 C: 빈칸을 내면 「사유가
              없었다」와 「이 꼬리표가 아직 안 실린 버전」이 같은 글자가 된다).
    """
    if field in UNTRAINED_MEMBER_FIELDS:
        return REASON_UNTRAINED
    # ⛔ **점수가 나온 축에는 사유가 없다.** 이 한 줄이 「규칙이 두 곳에 적히는 것」을 구조적으로
    # 막는다 — 아래 절들이 점수 함수의 조건을 손으로 되짚는 이상, 언젠가 한쪽만 바뀐다.
    # 그때 이 게이트가 없으면 **점수가 멀쩡한 축에 사유가 붙는다**(구현 당일 실제로 그랬다:
    # `charm_sign == 0`인데 거리 성분이 살아 있어 점수가 났는데도 `부호0[total_charm]`이
    # 붙었고, `tests/test_fusion_engine_axis_exit_reason.py`의 일치 시험이 그것을 잡았다).
    # 비용은 그 사이클 점수를 한 번 더 계산하는 것뿐이고, 이 함수는 **이탈이 난 분에만**
    # 불린다(하루 상한 20건 — `2026-08-21-fix7-member-exit-is-an-event`).
    if getattr(build_member_scores(inputs), field, None) is not None:
        return REASON_UNKNOWN
    if field == "regime_hmm":
        return "원재료없음[regime_state]" if inputs.regime_state is None else REASON_UNKNOWN
    if field == "options_flow":
        missing, zero_sign = _options_flow_absence(inputs)
        parts = []
        if missing:
            parts.append(f"원재료없음[{'·'.join(missing)}]")
        if zero_sign:
            parts.append(f"부호0[{'·'.join(zero_sign)}]")
        return " ".join(parts) or REASON_UNKNOWN
    if field == "orderflow_ofi_vpin":
        # ⚠ 이 멤버는 **부호0으로는 None이 되지 않는다** — `_orderflow_ofi_vpin_score`는 값이
        # 있으면 부호가 0이어도 성분에 담는다(0.0을 돌려준다). 그래서 갈래가 하나뿐이다.
        missing = [
            name for name, value in (("ofi", inputs.ofi), ("queue_imbalance", inputs.queue_imbalance))
            if value is None
        ]
        return f"원재료없음[{'·'.join(missing)}]" if len(missing) == 2 else REASON_UNKNOWN
    if field == "flow_position":
        return "원재료없음[foreign_net_flow]" if inputs.foreign_net_flow is None else REASON_UNKNOWN
    return REASON_UNKNOWN


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
