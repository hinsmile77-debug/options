from mahdi.engines.regime import RegimeLabel, RegimeState
from mahdi.fusion.signal_layer import SignalInputs, build_member_scores
from mahdi.fusion import signal_layer


def _regime_state(regime: RegimeLabel, stability_flag: bool = True) -> RegimeState:
    prob_vector = [0.0] * 8
    prob_vector[regime] = 0.9
    return RegimeState(regime=regime, prob_vector=tuple(prob_vector), stability_flag=stability_flag)


def test_all_none_inputs_yield_all_none_scores():
    scores = build_member_scores(SignalInputs())
    assert scores.regime_hmm is None
    assert scores.xgboost_tabular is None
    assert scores.lstm_temporal is None
    assert scores.options_flow is None
    assert scores.orderflow_ofi_vpin is None
    assert scores.flow_position is None


def test_regime_hmm_trend_up_strong_scores_positive():
    scores = build_member_scores(SignalInputs(regime_state=_regime_state(RegimeLabel.TREND_UP_STRONG)))
    assert scores.regime_hmm == 1.0


def test_regime_hmm_unstable_trend_is_dampened():
    scores = build_member_scores(
        SignalInputs(regime_state=_regime_state(RegimeLabel.TREND_DOWN_STRONG, stability_flag=False))
    )
    assert scores.regime_hmm == -0.5


def test_regime_hmm_range_regime_has_no_direction():
    scores = build_member_scores(SignalInputs(regime_state=_regime_state(RegimeLabel.RANGE_BALANCED)))
    assert scores.regime_hmm == 0.0


def test_xgboost_and_lstm_are_always_none():
    scores = build_member_scores(
        SignalInputs(regime_state=_regime_state(RegimeLabel.TREND_UP_STRONG), ofi=10.0)
    )
    assert scores.xgboost_tabular is None
    assert scores.lstm_temporal is None


def test_options_flow_positive_gex_reverts_toward_flip():
    # spot(105) > gamma_flip(100), 양수 GEX -> flip 방향(하락)으로 되돌아감 = 음수 방향
    scores = build_member_scores(SignalInputs(gex=1000.0, gamma_flip=100.0, spot=105.0))
    assert scores.options_flow == -1.0


def test_options_flow_negative_gex_extends_away_from_flip():
    # spot(105) > gamma_flip(100), 음수 GEX -> flip에서 계속 멀어짐 = 양수 방향
    scores = build_member_scores(SignalInputs(gex=-1000.0, gamma_flip=100.0, spot=105.0))
    assert scores.options_flow == 1.0


def test_options_flow_charm_only_counted_when_active():
    inactive = build_member_scores(
        SignalInputs(total_charm=5.0, charm_active=False)
    )
    active = build_member_scores(SignalInputs(total_charm=5.0, charm_active=True))
    assert inactive.options_flow is None
    assert active.options_flow == 1.0


def test_orderflow_ofi_vpin_averages_ofi_and_queue_imbalance():
    scores = build_member_scores(SignalInputs(ofi=5.0, queue_imbalance=-0.2))
    assert scores.orderflow_ofi_vpin == 0.0  # +1과 -1의 평균


def test_orderflow_ofi_vpin_none_when_no_inputs():
    scores = build_member_scores(SignalInputs())
    assert scores.orderflow_ofi_vpin is None


def test_flow_position_from_foreign_net_flow_sign():
    assert build_member_scores(SignalInputs(foreign_net_flow=-500.0)).flow_position == -1.0
    assert build_member_scores(SignalInputs(foreign_net_flow=500.0)).flow_position == 1.0
    assert build_member_scores(SignalInputs()).flow_position is None


# ===========================================================================
# 부호 규약 감사 — 2026-08-10 (08-11 판정용 하네스)
#
# ## 왜 이 절이 생겼는가
#
# `flow_position ↔ options_flow` 부호 일치율이 2영업일 연속 20% 밑이었다
# (08-06 74.6% → 08-07 13.1% → 08-10 18.5%). 무작위라면 50% 근처여야 한다.
#
# **임계를 걸지 않는다** — 정상 변동폭을 모르는 상태에서 임계를 먼저 정하면 그 임계가 곧
# 결론이 된다(08-05 스팟 괴리율에서 같은 실수를 했다). 대신 **두 멤버가 각각 무엇을 말하기로
# 돼 있는지를 고정 케이스로 박제**해, 낮은 일치율을 볼 때 "규약이 뒤집혔나"를 **즉답**할 수
# 있게 한다. 규약이 맞다면 낮은 일치율은 결함이 아니라 시장 사실이다.
#
# ## 08-10 실측이 알려준 것 (이 테스트들의 근거)
#
#   GEX 부호   options_flow   flow_position   분
#   GEX<0        −               +            308     ← 대부분이 여기
#   GEX<0        +               +             64
#   GEX<0        −               −              6
#   GEX<0        0               +              4
#
# **그날은 하루 종일 GEX<0이었다**(GEX>=0인 분이 0). 그리고 외국인은 종일 순매수(+)였고
# 스팟은 기준선 아래였다. 즉 두 멤버는 **서로 다른 것을 정직하게 말하고 있었다** —
# 수급은 사는데, 딜러가 숏감마라 하락이 증폭되는 국면이다. 같은 부호가 나올 이유가 없다.
#
# 그러므로 **낮은 일치율 자체는 규약 위반의 증거가 아니다.** 아래 케이스가 깨질 때만
# 규약을 의심한다.
# ===========================================================================


def _of(gex, spot, reference, *, wall=False):
    """options_flow 점수 한 개 — 기준선을 flip으로 줄지 wall로 줄지 고른다."""
    kwargs = {"gamma_wall": reference} if wall else {"gamma_flip": reference}
    return build_member_scores(SignalInputs(gex=gex, spot=spot, **kwargs)).options_flow


def test_options_flow_reference_is_the_single_source_for_score_and_record():
    """2026-09-03(감마월 정의·매핑 점검 §1 / 이상점 5) — 기록과 판단이 갈릴 수 없어야 한다.

    `main._build_signal_inputs()`는 이 함수로 `signal_decisions.gamma_reference_source`
    (마이그레이션 037)를 채우고, `_options_flow_score()`는 **같은 함수**로 기준선을 고른다.
    복사본이 아니라 같은 호출이라는 것을 여기서 못박는다 — 복사하면 아래 게이트 케이스에서
    기록만 살아남는다.
    """
    from mahdi.fusion.signal_layer import options_flow_reference

    assert options_flow_reference(1030.0, 1025.0) == (1030.0, "flip")
    # flip이 없으면 월로 떨어진다 — 08-04 이후 실동작 경로다.
    assert options_flow_reference(None, 1025.0) == (1025.0, "wall")
    assert options_flow_reference(None, None) == (None, "none")


def test_options_flow_reference_reports_none_when_the_fallback_gate_is_off(monkeypatch):
    """게이트를 끄면 월이 있어도 «기준선 없음»이다 — 기록도 그렇게 남아야 한다.

    이 케이스가 복사본을 잡는다: 기록 쪽이 규칙을 베껴 적었다면 `gamma_wall`이 non-NULL인데
    `gamma_reference_source='wall'`로 남아, 08-04 이전으로 되돌린 날의 이력이 되돌리지 않은
    날과 구분되지 않는다.
    """
    from mahdi.fusion.signal_layer import options_flow_reference

    monkeypatch.setattr("mahdi.fusion.signal_layer.OPTIONS_FLOW_GAMMA_WALL_FALLBACK", False)

    assert options_flow_reference(None, 1025.0) == (None, "none")
    assert build_member_scores(SignalInputs(gex=-1.0, spot=1030.0, gamma_wall=1025.0)).options_flow is None


def test_sign_convention_options_flow_says_revert_when_dealers_are_long_gamma():
    """GEX>=0(딜러 롱감마) → 스팟이 기준선에서 멀어진 **반대** 방향을 가리킨다(회귀)."""
    assert _of(gex=+1.0, spot=105.0, reference=100.0) == -1.0   # 위에 있으면 아래로
    assert _of(gex=+1.0, spot=95.0, reference=100.0) == +1.0    # 아래에 있으면 위로


def test_sign_convention_options_flow_says_extend_when_dealers_are_short_gamma():
    """GEX<0(딜러 숏감마) → 스팟이 벌어진 **그** 방향을 가리킨다(증폭).

    08-10이 종일 이 국면이었고, 스팟이 기준선 아래라 `options_flow`가 −로 고정됐다.
    """
    assert _of(gex=-1.0, spot=105.0, reference=100.0) == +1.0
    assert _of(gex=-1.0, spot=95.0, reference=100.0) == -1.0


def test_sign_convention_flow_position_is_the_raw_foreign_flow_sign():
    """`flow_position`은 외국인 순매수 부호 **그대로**다 — 반전이 없다."""
    assert build_member_scores(SignalInputs(foreign_net_flow=500.0)).flow_position == +1.0
    assert build_member_scores(SignalInputs(foreign_net_flow=-500.0)).flow_position == -1.0


def test_the_two_members_are_allowed_to_disagree_by_construction():
    """**08-10의 국면을 그대로 재현한다** — 낮은 일치율이 규약 위반이 아님을 박제한다.

    외국인 순매수(+) · GEX<0 · 스팟이 기준선 아래 → `flow_position`=+1, `options_flow`=−1.
    두 멤버가 반대 부호를 내는 것이 **설계상 정상**이다. 이 케이스가 깨지면(둘이 같은 부호가
    되면) 그때가 규약이 바뀐 것이다.
    """
    scores = build_member_scores(SignalInputs(
        gex=-1.0, spot=95.0, gamma_wall=100.0, foreign_net_flow=500.0,
    ))
    assert scores.flow_position == +1.0
    assert scores.options_flow == -1.0


def test_options_flow_falls_back_to_the_gamma_wall_and_that_is_its_real_reference():
    """**이 멤버는 태어난 뒤 한 번도 gamma_flip을 기준선으로 쓴 적이 거의 없다.**

    `signal_decisions.gamma_flip` 실측(08-03~08-10, 2,944 판단): 08-05의 22건을 빼면 전부 NULL.
    즉 실동작 기준선은 사실상 **감마 월**이고, docstring이 말하는 "Gamma Flip"이 아니다.
    이것은 결함이 아니라 2026-08-04 Fix#3이 명시적으로 고른 폴백이지만, **부호를 해석할 때
    무엇과 비교한 것인지 착각하면 안 되므로** 여기에 박제한다.
    """
    assert signal_layer.OPTIONS_FLOW_GAMMA_WALL_FALLBACK is True
    # flip이 없어도 wall만으로 점수가 나온다.
    assert _of(gex=-1.0, spot=95.0, reference=100.0, wall=True) == -1.0
    # flip이 있으면 flip이 이긴다(폴백은 어디까지나 폴백이다).
    both = build_member_scores(SignalInputs(
        gex=-1.0, spot=95.0, gamma_flip=90.0, gamma_wall=100.0,
    ))
    assert both.options_flow == +1.0, "flip(90) 기준이면 스팟 95는 위쪽이라 증폭은 +다"


def test_zero_distance_produces_no_opinion_not_a_fake_zero():
    """스팟이 기준선과 정확히 같으면 **의견 없음**(None)이지 중립 0이 아니다.

    0은 중립이지 의견이 아니다 — 부호 일치율의 분모에서 빠져야 한다(§14-3 규약).
    """
    assert _of(gex=-1.0, spot=100.0, reference=100.0) is None
