import contextlib
import io
from datetime import date, time

import pytest

from mahdi.features.options_intel import (
    GammaMapEngine,
    OptionLeg,
    atm_straddle_vrp,
    calculate_gex,
    calculate_vrp,
    find_gamma_flip,
    gamma_walls,
    legs_by_expiry,
    legs_from_chain_rows,
    pin_risk,
    monthly_atm_iv,
    monthly_expiry,
    signal_book_legs,
    usable_for_black_scholes,
    vanna_charm_drift,
)


def _leg(strike: float, opt: str, oi: float, *, iv: float = 0.18, t_years: float = 0.05) -> OptionLeg:
    return OptionLeg(strike=strike, option_type=opt, oi=oi, iv=iv, t_years=t_years, gamma=0.0)


def _flip_legs() -> list[OptionLeg]:
    """스팟 350 기준 338~341 사이에서 GEX 부호가 바뀌는, 실제 체인 모양(행사가 3개 x C/P)의 구성.

    순 익스포저(콜OI - 풋OI)는 340에서 -500, 350에서 0, 360에서 +1500 — 낮은 쪽이 풋 우세,
    높은 쪽이 콜 우세인 전형적인 배치다. GAMMA_FLIP_MIN_LEGS(6)를 만족하는 최소 크기이기도 하다.
    """
    return [
        _leg(340, "c", 100), _leg(340, "p", 600),
        _leg(350, "c", 300), _leg(350, "p", 300),
        _leg(360, "c", 1600), _leg(360, "p", 100),
    ]


def _no_flip_legs() -> list[OptionLeg]:
    """전 구간에서 콜 우세라 부호가 바뀌지 않는 구성 — flip이 탐색 범위 밖인 정상 케이스."""
    return [
        _leg(340, "c", 100), _leg(340, "p", 0),
        _leg(350, "c", 100), _leg(350, "p", 0),
        _leg(360, "c", 100), _leg(360, "p", 0),
    ]


def test_calculate_gex_empty_is_zero():
    assert calculate_gex([], spot=350) == 0.0


def test_legs_from_chain_rows_converts_db_rows_to_option_legs():
    rows = [
        {"strike": 350.0, "option_type": "C", "oi": 100.0, "iv": 0.18, "gamma": 0.02,
         "gex": 123.0, "expiry": date(2026, 8, 13), "timestamp": None},
        {"strike": 350.0, "option_type": "P", "oi": 50.0, "iv": 0.20, "gamma": 0.015,
         "gex": -80.0, "expiry": date(2026, 8, 13), "timestamp": None},
    ]
    legs = legs_from_chain_rows(rows, today=date(2026, 7, 28))

    assert len(legs) == 2
    assert legs[0] == OptionLeg(
        strike=350.0, option_type="c", oi=100.0, iv=0.18, t_years=pytest.approx(16 / 365), gamma=0.02
    )
    assert legs[1].option_type == "p"


def test_legs_from_chain_rows_skips_rows_without_expiry():
    rows = [{"strike": 350.0, "option_type": "C", "oi": 1.0, "iv": 0.1, "gamma": 0.01, "expiry": None}]
    assert legs_from_chain_rows(rows, today=date(2026, 7, 28)) == []


def test_legs_from_chain_rows_empty_input_is_empty():
    assert legs_from_chain_rows([], today=date(2026, 7, 28)) == []


def test_legs_from_chain_rows_excludes_already_expired_rows():
    # 2026-08-03 §2-2: docstring은 처음부터 "만기가 지난 레그는 제외한다"고 적혀 있었는데 코드는
    # max(..., 0)으로 t_years만 0으로 clamp해 조용히 통과시켰다 — 라이브 실측 246레그 중 156개가
    # 이미 만기가 지난 레그였다. 문서 쪽이 옳으므로 코드를 문서에 맞췄다.
    rows = [{"strike": 350.0, "option_type": "C", "oi": 1.0, "iv": 0.1, "gamma": 0.01, "expiry": date(2026, 7, 1)}]
    assert legs_from_chain_rows(rows, today=date(2026, 7, 28)) == []


def test_legs_from_chain_rows_keeps_same_day_expiry():
    # 만기 당일 북(위클리 월/목)은 핀 리스크의 주 무대라 GEX에는 반드시 들어가야 한다.
    # t_years=0이라 BS 감마는 못 구하지만 calculate_gex()는 저장된 gamma를 쓰므로 문제 없고,
    # find_gamma_flip()은 usable_for_black_scholes()가 따로 걸러낸다.
    rows = [{"strike": 350.0, "option_type": "C", "oi": 1.0, "iv": 0.1, "gamma": 0.01, "expiry": date(2026, 7, 28)}]
    legs = legs_from_chain_rows(rows, today=date(2026, 7, 28))
    assert len(legs) == 1
    assert legs[0].t_years == 0.0


def test_calculate_gex_call_positive_put_negative():
    call_leg = OptionLeg(strike=350, option_type="c", oi=100, iv=0.18, t_years=0.05, gamma=0.01)
    put_leg = OptionLeg(strike=350, option_type="p", oi=50, iv=0.18, t_years=0.05, gamma=0.01)
    spot = 350
    s_term = spot**2 / 100
    expected = 0.01 * 100 * 250_000 * s_term - 0.01 * 50 * 250_000 * s_term
    assert calculate_gex([call_leg, put_leg], spot) == pytest.approx(expected)


def test_find_gamma_flip_none_when_calls_only_always_positive():
    # 콜만 있으면 감마·OI·S^2 항이 전 구간에서 양수 → 부호 전환 없음
    assert find_gamma_flip(_no_flip_legs(), spot=350) is None


def test_find_gamma_flip_detects_sign_change():
    # put OI가 낮은 스팟 쪽에, call OI가 높은 스팟 쪽에 몰려있는 실제 시장과 유사한 구성.
    # 사전에 그리드를 스캔해 338~341 사이에서 부호가 바뀌는 것을 확인한 파라미터.
    flip = find_gamma_flip(_flip_legs(), spot=350)
    assert flip is not None
    assert 335 < flip < 345


def test_find_gamma_flip_does_not_leak_vollib_print_to_stdout():
    # 2026-07-08 실측: vollib.ref_python(C 확장 미설치 폴백)의 d1()에 디버그용 print('')이 남아
    # 있어 COCKPIT 하루 로그(667,663줄)의 99% 이상이 이 빈 줄이었다. 실제로 vollib를 호출하는
    # 경로여야 회귀를 잡으므로 **계산 가능한 레그**를 쓴다(iv=0/t_years=0 레그는 2026-08-03
    # §2-1 이후 계산 전에 배제돼 vollib에 닿지도 않는다 — 그걸로 테스트하면 거짓 통과다).
    legs = _flip_legs()
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        find_gamma_flip(legs, spot=350)
    assert captured.getvalue() == ""


def test_find_gamma_flip_excludes_zero_iv_legs_instead_of_poisoning_the_sum(caplog):
    """2026-08-03 §2-1 회귀 — iv=0 레그 하나가 전체 곡선을 NaN으로 만들어선 안 된다.

    수정 전 코드는 `gex_at()`에서 레그별 감마를 **합산**했기 때문에, iv=0인 레그가 하나만 섞여도
    그 그리드 포인트의 합계 전체가 NaN이 됐다. 그리고 NaN은 `values[i-1]*values[i] < 0`을 항상
    False로 만들어 함수가 예외도 경고도 없이 None으로 떨어졌다 — 라이브 실측에서 41개 그리드가
    전부 NaN이었고, `signal_decisions` 전 이력에서 `available_member_count >= 3`인 행이 0건
    (앙상블 멤버 options_flow가 한 번도 활성화된 적 없음)이었던 직접적 원인이다.
    """
    legs = [*_flip_legs(), _leg(350, "c", 100, iv=0.0)]
    flip = find_gamma_flip(legs, spot=350)
    assert flip is not None, "iv=0 레그 하나 때문에 flip이 사라지면 안 된다"
    assert 335 < flip < 345
    assert not caplog.records, "정상 산출 경로에서는 경고를 남기지 않는다"


def test_find_gamma_flip_returns_none_when_the_whole_book_has_no_open_interest():
    """2026-08-04(운영점검보고서 §2-9 후속) — OI가 전부 0인 북에서 **허수 flip 레벨**이 나왔다.

    종전 코드는 `if v_prev == 0: return grid[i_prev]`로 처음 만난 0을 그대로 flip으로 돌려줬다.
    OI가 전부 0이면 GEX(S)가 모든 구간에서 0이므로 이 분기가 **항상 첫 그리드 점**
    (= spot x (1 - search_pct))을 반환한다. 08-04 실측: weekly_mon(2026-08-10, 롤오버 직후라
    OI 미형성) 북에서 스팟 1000.03에 대해 `감마플립 950.03`이 나왔다 — 정확히 5% 아래,
    즉 탐색 격자의 왼쪽 끝이다. 사람이 읽으면 시장 구조로 오해하는 완전한 허수다.
    """
    legs = [
        OptionLeg(strike=k, option_type=t, oi=0.0, iv=0.18, t_years=0.02, gamma=0.01)
        for k in (340.0, 345.0, 350.0, 355.0, 360.0)
        for t in ("c", "p")
    ]
    assert find_gamma_flip(legs, spot=350) is None


def test_find_gamma_flip_ignores_a_tangent_zero_that_does_not_cross():
    """0을 스치기만 하고 같은 부호로 돌아오면 교차가 아니다 — flip을 지어내지 않는다."""
    # 콜만 있는 체인은 전 구간 양수고, OI 0인 레그를 섞어도 곡선이 0을 스치지 않는다.
    legs = [*_no_flip_legs(), _leg(350, "c", 0.0)]
    assert find_gamma_flip(legs, spot=350) is None


def test_find_gamma_flip_still_finds_a_real_crossing_next_to_an_exact_zero():
    """회귀 방지: 0 처리를 고치면서 **진짜 교차**까지 놓치면 안 된다."""
    legs = [*_flip_legs(), _leg(350, "c", 0.0), _leg(350, "p", 0.0)]
    flip = find_gamma_flip(legs, spot=350)
    assert flip is not None
    assert 335 < flip < 345


def test_find_gamma_flip_warns_when_too_few_usable_legs(caplog):
    # 조용한 실패가 §2-1 버그를 넉 달간 가렸다 — 산출 불가는 반드시 로그에 남는다.
    legs = [OptionLeg(strike=350, option_type="c", oi=100, iv=0.0, t_years=0.0, gamma=0.0)]
    assert find_gamma_flip(legs, spot=350) is None
    assert any("감마플립 산출 불가" in r.message for r in caplog.records)


def test_find_gamma_flip_out_of_range_is_silent(caplog):
    # 반대로 "탐색 범위 안에 flip이 없다"는 정상적인 결과이므로 로그를 남기지 않는다
    # (매분 경고가 나오면 진짜 경고가 파묻힌다).
    assert find_gamma_flip(_no_flip_legs(), spot=350) is None
    assert not caplog.records


# ===== 2026-08-05 §2-5 — 레그 범위 밖 감마플립 =====
#
# 08-05에 flip이 처음으로 0%를 벗어나 22분 산출됐는데 **21건이 수집 행사가 창 밖**이었다.
# 아래 세 테스트는 그 사건의 세 국면을 각각 고정한다: 기각 / 정당한 경계 통과 / 가림 방지.


def _iv_jitter_legs(top_call_oi: float = 400, extra_top_strike: bool = False) -> list[OptionLeg]:
    """08-05 실측 모양 — **행사가별 IV 지터**가 레그 범위 밖에 허수 부호 전환을 만든다.

    잔존만기가 짧으면(만기 임박) 감마는 행사가에서 급격히 꺼지므로, 레그 범위에서 멀어질수록
    **IV가 높은 레그**가 곡선을 지배한다(고IV일수록 감마 꼬리가 두껍다). 그 레그의 콜/풋 순
    포지션이 북 전체와 반대면 레그가 하나도 없는 구간에서 GEX 부호가 뒤집힌다.

    이것이 가상의 시나리오가 아닌 이유: KIS `hts_ints_vltl`은 행사가 격자에 따라 계통적으로
    튄다(08-05 `e02fae8` 실측 — 같은 분·같은 북에서 2.5의 홀수배 콜 0.57~0.63 vs 5의 배수
    0.87~0.89). 즉 **측정 지터가 만든 부호 전환**이지 시장 구조가 아니다.
    """
    legs = [
        OptionLeg(strike=1042.5, option_type="c", oi=3000, iv=1.4, t_years=0.004, gamma=0.0),
        OptionLeg(strike=1042.5, option_type="p", oi=10, iv=1.4, t_years=0.004, gamma=0.0),
    ]
    for strike, call_oi, put_oi in [
        (1045.0, 30, 500), (1047.5, 60, 900), (1050.0, 40, 300), (1052.5, top_call_oi, 80),
    ]:
        legs += [
            OptionLeg(strike=strike, option_type="c", oi=call_oi, iv=0.55, t_years=0.004, gamma=0.0),
            OptionLeg(strike=strike, option_type="p", oi=put_oi, iv=0.55, t_years=0.004, gamma=0.0),
        ]
    if extra_top_strike:
        # 레그 범위를 위로 넓혀 두 번째 교차(1060.86)를 허용 구간 안으로 들인다.
        legs += [
            OptionLeg(strike=1062.5, option_type="c", oi=1, iv=0.55, t_years=0.004, gamma=0.0),
            OptionLeg(strike=1062.5, option_type="p", oi=1, iv=0.55, t_years=0.004, gamma=0.0),
        ]
    return legs


def test_find_gamma_flip_rejects_a_crossing_far_outside_the_collected_strikes(caplog):
    """수집 행사가 1042.5~1052.5(허용 ±2.5)인데 교차는 1031.9와 1060.86 — 둘 다 밖이다.

    08-05 실측에서는 이 경로가 하루 22건 중 21건을 만들었고, 그 21건의 `flip / spot`이 전부
    0.95~0.99에 몰려 있었다(= 탐색 그리드 가장자리). 08-04에 고친 허수 flip과 **같은 자리에서
    다른 경로로 재발한 것**이라 조용히 None으로 떨어뜨리지 않고 마커를 남긴다.
    """
    assert find_gamma_flip(_iv_jitter_legs(), spot=1048.0) is None
    assert any("감마플립 기각(레그 범위 밖)" in r.message for r in caplog.records), (
        "기각은 반드시 로그로 드러나야 한다 — 조용한 기각은 08-03 §2-1의 재현이다"
    )
    assert sum("감마플립 기각" in r.message for r in caplog.records) == 1, (
        "레그 없는 구간은 부호가 여러 번 튈 수 있다 — 호출당 1줄로 묶어 §2-4의 로그 폭증을 만들지 않는다"
    )


def test_find_gamma_flip_allows_a_crossing_just_outside_the_collected_strikes(caplog):
    """최외곽 행사가 **바로 바깥**은 기각하지 않는다 — 거기엔 그 레그의 감마가 실재한다.

    행사가 340/350/360(간격 10)의 진짜 flip은 338.25로 최저 행사가에서 1.75포인트 아래인데,
    그 지점의 1σ 이동폭은 약 14포인트라 340 레그의 감마가 충분히 살아 있다. 허용치를
    **행사가 간격 단위**로 잡은 것이 이 구분을 만든다(0.18간격 통과 vs 08-05의 4.5~34.5간격 기각).
    경계를 [min, max]로 딱 자르면 이 정당한 flip이 사라진다.
    """
    flip = find_gamma_flip(_flip_legs(), spot=350)
    assert flip is not None and 335 < flip < 340
    assert not caplog.records, "정당한 경계 통과에는 로그를 남기지 않는다"


def test_find_gamma_flip_skips_an_out_of_range_crossing_to_find_an_in_range_one(caplog):
    """범위 밖 교차가 **범위 안 교차를 가려선 안 된다** — 그리드를 끝까지 훑는다.

    같은 곡선에 교차가 1031.9(밖)와 1060.86(안) 둘 있다. 왼쪽 끝에서 첫 교차를 만났다고
    거기서 멈추면(=기각하고 None 반환) 진짜 flip을 놓친다. 이 테스트가 없으면 "범위 밖이면
    None"이라는 순진한 구현이 통과해 버린다.
    """
    flip = find_gamma_flip(_iv_jitter_legs(extra_top_strike=True), spot=1048.0)
    assert flip is not None and 1055 < flip < 1065
    assert not caplog.records, "범위 안 flip을 찾았으면 기각 로그를 남기지 않는다"


def test_usable_for_black_scholes_requires_positive_iv_time_strike():
    assert usable_for_black_scholes(OptionLeg(strike=350, option_type="c", oi=1, iv=0.18, t_years=0.05, gamma=0.0))
    assert not usable_for_black_scholes(OptionLeg(strike=350, option_type="c", oi=1, iv=0.0, t_years=0.05, gamma=0.0))
    assert not usable_for_black_scholes(OptionLeg(strike=350, option_type="c", oi=1, iv=0.18, t_years=0.0, gamma=0.0))
    assert not usable_for_black_scholes(OptionLeg(strike=0, option_type="c", oi=1, iv=0.18, t_years=0.05, gamma=0.0))


def test_gamma_walls_ranks_by_exposure():
    legs = [
        OptionLeg(strike=350, option_type="c", oi=100, iv=0.18, t_years=0.05, gamma=0.02),
        OptionLeg(strike=355, option_type="p", oi=10, iv=0.18, t_years=0.05, gamma=0.01),
        OptionLeg(strike=345, option_type="c", oi=500, iv=0.18, t_years=0.05, gamma=0.03),
    ]
    walls = gamma_walls(legs, spot=350, top_n=2)
    assert len(walls) == 2
    assert walls[0][0] == 345  # 가장 큰 익스포저 행사가가 첫번째
    assert walls[0][1] > walls[1][1]


def test_gamma_walls_empty_legs():
    assert gamma_walls([], spot=350) == []


def test_vanna_charm_drift_aggregates_and_flags_charm_window():
    legs = [
        OptionLeg(strike=350, option_type="c", oi=100, iv=0.18, t_years=0.05, gamma=0.01, vanna=0.5, charm=-0.1),
        OptionLeg(strike=345, option_type="p", oi=50, iv=0.18, t_years=0.05, gamma=0.01, vanna=-0.2, charm=0.2),
    ]
    before = vanna_charm_drift(legs, now=time(13, 0))
    after = vanna_charm_drift(legs, now=time(14, 30))

    assert before["charm_active"] is False
    assert after["charm_active"] is True
    assert before["total_vanna"] == pytest.approx(0.5 * 100 + (-0.2) * 50)
    assert before["total_charm"] == pytest.approx(-0.1 * 100 + 0.2 * 50)


def test_calculate_vrp_sign():
    assert calculate_vrp(iv=0.20, realized_vol=0.15) == pytest.approx(0.05)
    assert calculate_vrp(iv=0.10, realized_vol=0.15) == pytest.approx(-0.05)


def test_gamma_map_engine_delegates_to_functions():
    engine = GammaMapEngine()
    legs = [OptionLeg(strike=350, option_type="c", oi=100, iv=0.18, t_years=0.05, gamma=0.01)]
    assert engine.calculate_gex(legs, spot=350) == calculate_gex(legs, spot=350)
    assert engine.gamma_walls(legs, spot=350) == gamma_walls(legs, spot=350)


# ===== 2026-08-03 §5-5: 북별 체인 스냅샷 =====


def test_legs_by_expiry_separates_books_instead_of_merging_them():
    """3개 북을 합산하면 만기별 정보가 서로를 덮는다 — 특히 만기 당일 북이 묻힌다."""
    rows = [
        {"strike": 350.0, "option_type": "C", "oi": 100.0, "iv": 0.18, "gamma": 0.02,
         "expiry": date(2026, 8, 13)},
        {"strike": 350.0, "option_type": "C", "oi": 50.0, "iv": 0.30, "gamma": 0.05,
         "expiry": date(2026, 8, 6)},
        {"strike": 350.0, "option_type": "P", "oi": 70.0, "iv": 0.40, "gamma": 0.08,
         "expiry": date(2026, 8, 6)},
    ]
    grouped = legs_by_expiry(rows, today=date(2026, 8, 3))

    assert list(grouped) == [date(2026, 8, 6), date(2026, 8, 13)]  # 만기 오름차순
    assert len(grouped[date(2026, 8, 6)]) == 2
    assert len(grouped[date(2026, 8, 13)]) == 1


def test_legs_by_expiry_keeps_the_same_exclusion_rules_as_the_flat_conversion():
    rows = [
        {"strike": 350.0, "option_type": "C", "oi": 1.0, "iv": 0.1, "gamma": 0.01, "expiry": None},
        {"strike": 350.0, "option_type": "C", "oi": 1.0, "iv": 0.1, "gamma": 0.01,
         "expiry": date(2026, 7, 1)},  # 이미 만기
    ]
    assert legs_by_expiry(rows, today=date(2026, 8, 3)) == {}


# ===== 2026-08-05 P0-2: 관측 루프와 COCKPIT이 공유하는 북 선택 =====


def test_signal_book_legs_picks_only_the_monthly_book():
    """먼슬리 북 하나만 — 위클리와 합산하면 GEX 부호가 상쇄로 좌우된다(08-04 §2-8).

    2026-08-11: 여기서 08-13이 뽑히는 이유가 "가장 먼 만기"에서 "그 달 두 번째 목요일"로
    바뀌었다. 이 케이스는 둘 다 같은 답을 내므로 그대로 통과한다 — 갈리는 케이스는 아래.
    """
    rows = [
        {"strike": 1045.0, "option_type": "C", "oi": 100.0, "iv": 0.18, "gamma": 0.02,
         "expiry": date(2026, 8, 13)},  # 먼슬리
        {"strike": 1045.0, "option_type": "P", "oi": 900.0, "iv": 0.40, "gamma": 0.09,
         "expiry": date(2026, 8, 6)},   # 위클리(목) — 잔존 1일이라 감마가 압도적
        {"strike": 1047.5, "option_type": "C", "oi": 800.0, "iv": 0.35, "gamma": 0.08,
         "expiry": date(2026, 8, 10)},  # 위클리(월)
    ]

    legs, expiry = signal_book_legs(rows, today=date(2026, 8, 5))

    assert expiry == date(2026, 8, 13)
    assert len(legs) == 1
    assert legs[0].strike == 1045.0
    assert legs[0].option_type == "c"


def test_signal_book_legs_returns_empty_without_usable_rows():
    assert signal_book_legs([], today=date(2026, 8, 5)) == ([], None)
    expired = [{"strike": 1045.0, "option_type": "C", "oi": 1.0, "iv": 0.1, "gamma": 0.01,
                "expiry": date(2026, 7, 1)}]
    assert signal_book_legs(expired, today=date(2026, 8, 5)) == ([], None)


# ===== 2026-08-11: 공휴일이 위클리를 먼슬리 뒤로 밀면 `max(expiry)`가 뒤집힌다 =====
#
# 실사고: 2026-08-13(목)이 8월물 만기인데 08-15 광복절이 토요일이라 08-17(월)이 대체공휴일이
# 됐고, 위클리(월) 만기가 08-18로 **먼슬리 뒤로** 밀렸다. `max()`가 위클리를 먼슬리로 지목했다.
# 아래 넷은 전부 `monthly_expiry()`를 `max()`로 되돌리면 깨진다 — 그것이 이 테스트들의 목적이다.


def test_monthly_expiry_prefers_the_second_thursday_over_the_farthest():
    # 08-13 = 2026년 8월의 두 번째 목요일(8월 1일이 토요일 → 목요일은 6일, 13일).
    assert monthly_expiry([date(2026, 8, 13), date(2026, 8, 18)]) == date(2026, 8, 13)
    # 순서와 중복에 흔들리지 않는다.
    assert monthly_expiry([date(2026, 8, 18), date(2026, 8, 13), date(2026, 8, 18)]) == date(2026, 8, 13)


def test_monthly_expiry_falls_back_to_the_farthest_when_no_second_thursday_is_present():
    """먼슬리가 이미 만기로 빠진 뒤(위클리만 남음)에는 종전 동작을 유지한다 — 지어내지 않는다."""
    assert monthly_expiry([date(2026, 8, 17), date(2026, 8, 24)]) == date(2026, 8, 24)
    assert monthly_expiry([]) is None


def test_signal_book_legs_picks_the_monthly_even_when_a_weekly_expires_later():
    """2026-08-11 실사고 형태. `max()`로 되돌리면 08-18(위클리)이 뽑혀 이 테스트가 깨진다."""
    rows = [
        {"strike": 979.0, "option_type": "C", "oi": 100.0, "iv": 0.79, "gamma": 0.02,
         "expiry": date(2026, 8, 13)},  # 먼슬리(8월물) — 두 번째 목요일
        {"strike": 979.0, "option_type": "C", "oi": 800.0, "iv": 0.62, "gamma": 0.08,
         "expiry": date(2026, 8, 18)},  # 위클리(월) — 대체공휴일로 먼슬리보다 뒤
    ]

    legs, expiry = signal_book_legs(rows, today=date(2026, 8, 11))

    assert expiry == date(2026, 8, 13)
    assert len(legs) == 1
    assert legs[0].iv == pytest.approx(0.79)


def test_monthly_atm_iv_does_not_alternate_when_the_weekly_book_appears_on_even_minutes():
    """R6의 본질 — 위클리가 격분에만 조회돼도 ATM IV는 **같은 값**이어야 한다.

    2026-08-11 실측 격차는 0.168이었다(먼슬리 0.7910 / 위클리 0.6229). R6이 2026-08-10에
    없앤 구형파(0.210)의 80%가 다른 원인으로 같은 자리에 돌아와 있었고, 하필 HMM이 처음
    라이브로 도는 날이었다. 이 테스트가 그 재발을 막는다.
    """
    monthly = [
        {"strike": 979.0, "option_type": "C", "iv": 0.7910, "expiry": date(2026, 8, 13)},
        {"strike": 979.0, "option_type": "P", "iv": 0.7910, "expiry": date(2026, 8, 13)},
    ]
    weekly = [
        {"strike": 979.0, "option_type": "C", "iv": 0.6229, "expiry": date(2026, 8, 18)},
        {"strike": 979.0, "option_type": "P", "iv": 0.6229, "expiry": date(2026, 8, 18)},
    ]

    odd_minute = monthly_atm_iv(monthly, spot=979.18)             # 홀수분 — 먼슬리 단독
    even_minute = monthly_atm_iv(monthly + weekly, spot=979.18)   # 짝수분 — 위클리가 섞인다

    assert odd_minute == pytest.approx(0.7910)
    assert even_minute == pytest.approx(odd_minute)


def test_pin_risk_is_computable_on_expiry_day_when_gamma_flip_is_not():
    # 만기 당일은 t_years=0이라 BS 감마가 정의되지 않는다 — 그런데 핀 리스크는 바로 그 북의 것이다.
    legs = [
        _leg(350, "c", 1000, t_years=0.0), _leg(350, "p", 1000, t_years=0.0),
        _leg(355, "c", 10, t_years=0.0), _leg(345, "p", 10, t_years=0.0),
    ]
    legs = [OptionLeg(l.strike, l.option_type, l.oi, l.iv, l.t_years, gamma=0.05) for l in legs]

    assert find_gamma_flip(legs, spot=350.0) is None  # BS 경로는 못 쓴다
    risk = pin_risk(legs, spot=350.0)
    assert risk is not None
    assert risk["strike"] == 350.0
    assert risk["concentration"] > 0.9  # 노출이 한 행사가에 몰려 있다
    assert risk["distance_pct"] == pytest.approx(0.0)


def test_pin_risk_returns_none_when_there_is_no_exposure():
    # OI가 전부 0이면(08-03 weekly_thu가 91% 그랬다) 지어내지 않는다.
    legs = [OptionLeg(350, "c", oi=0.0, iv=0.2, t_years=0.01, gamma=0.05)]
    assert pin_risk(legs, spot=350.0) is None
    assert pin_risk([], spot=350.0) is None


# ===== 2026-08-05(운영점검보고서 2026-08-05 §2 이상점 1 / Fix#1) — ATM 스트래들 VRP =====


def _chain_row(strike: float, opt: str, iv: float, rv: float | None = 0.78) -> dict:
    return {"strike": strike, "option_type": opt, "iv": iv, "rv_5d": rv}


def test_atm_straddle_vrp_averages_call_and_put_at_the_nearest_strike():
    rows = [_chain_row(1045.0, "C", 0.86), _chain_row(1045.0, "P", 0.90)]

    # mean(0.86, 0.90) - 0.78 = 0.10
    assert atm_straddle_vrp(rows, spot=1045.4) == pytest.approx(0.10)


def test_atm_straddle_vrp_picks_the_strike_nearest_to_spot():
    rows = [
        _chain_row(1045.0, "C", 0.86), _chain_row(1045.0, "P", 0.90),
        _chain_row(1050.0, "C", 0.70), _chain_row(1050.0, "P", 0.72),
    ]

    assert atm_straddle_vrp(rows, spot=1049.0) == pytest.approx((0.70 + 0.72) / 2 - 0.78)


def test_atm_straddle_vrp_breaks_midpoint_ties_deterministically():
    # 스팟이 두 행사가의 정확한 중점이면 낮은 행사가를 택한다 — 같은 입력에 늘 같은 답이어야
    # 백테스트와 라이브가 갈리지 않는다.
    rows = [
        _chain_row(1045.0, "C", 0.86), _chain_row(1045.0, "P", 0.90),
        _chain_row(1050.0, "C", 0.70), _chain_row(1050.0, "P", 0.72),
    ]

    assert atm_straddle_vrp(rows, spot=1047.5) == pytest.approx((0.86 + 0.90) / 2 - 0.78)


def test_atm_straddle_vrp_requires_both_call_and_put():
    """08-05 09:18 실측 재현 — ATM(1047.5)에 콜만 iv>0이었고 그 콜 IV가 0.5423이라
    VRP가 −0.24("저평가")로 나왔다. 그 값 하나가 팔레트를 `small_strangle_buy`로 보낸다.
    KIS `hts_ints_vltl`은 행사가 격자에 따라 계통적으로 튀므로(홀수배 0.57~0.63 vs
    5의 배수 0.87~0.89) 단일 레그로는 행사가 한 칸에 부호가 뒤집힌다."""
    assert atm_straddle_vrp([_chain_row(1047.5, "C", 0.5423)], spot=1047.1) is None
    assert atm_straddle_vrp([_chain_row(1047.5, "P", 0.9278)], spot=1047.1) is None


def test_atm_straddle_vrp_ignores_legs_with_non_positive_iv():
    # iv=0은 KIS 미제공이다 — 평균에 넣으면 ATM IV를 절반으로 끌어내린다.
    rows = [_chain_row(1045.0, "C", 0.0), _chain_row(1045.0, "P", 0.90)]
    assert atm_straddle_vrp(rows, spot=1045.0) is None


def test_atm_straddle_vrp_returns_none_when_realized_vol_is_missing_or_zero():
    """08-05 실측: 위클리 두 북(08-06/08-10)은 `rv_5d`가 **전 행 0**이었다. 그대로 빼면
    VRP = IV(1.18까지)가 돼 그 북은 항상 극단적 고평가로 판정된다."""
    assert atm_straddle_vrp(
        [_chain_row(1045.0, "C", 0.86, rv=0.0), _chain_row(1045.0, "P", 0.90, rv=0.0)], spot=1045.0
    ) is None
    assert atm_straddle_vrp(
        [_chain_row(1045.0, "C", 0.86, rv=None), _chain_row(1045.0, "P", 0.90, rv=None)], spot=1045.0
    ) is None


def test_atm_straddle_vrp_returns_none_without_spot_or_chain():
    rows = [_chain_row(1045.0, "C", 0.86), _chain_row(1045.0, "P", 0.90)]
    assert atm_straddle_vrp(rows, spot=None) is None
    assert atm_straddle_vrp([], spot=1045.0) is None


def test_atm_straddle_vrp_sign_maps_to_palette_columns():
    """팔레트가 이 부호로 §11.4 매트릭스의 열을 고른다 — 밴드는 ±0.02(2 변동성 포인트)."""
    from mahdi.fusion.strategy_palette import _vrp_state

    overpriced = atm_straddle_vrp([_chain_row(1045.0, "C", 0.86), _chain_row(1045.0, "P", 0.90)], 1045.0)
    underpriced = atm_straddle_vrp([_chain_row(1045.0, "C", 0.60), _chain_row(1045.0, "P", 0.62)], 1045.0)
    fair = atm_straddle_vrp([_chain_row(1045.0, "C", 0.78), _chain_row(1045.0, "P", 0.78)], 1045.0)

    assert _vrp_state(overpriced, 0.02) == "overpriced"
    assert _vrp_state(underpriced, 0.02) == "underpriced"
    assert _vrp_state(fair, 0.02) == "fair"
