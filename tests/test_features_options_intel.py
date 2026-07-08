import contextlib
import io
from datetime import time

import pytest

from mahdi.features.options_intel import (
    GammaMapEngine,
    OptionLeg,
    calculate_gex,
    calculate_vrp,
    find_gamma_flip,
    gamma_walls,
    vanna_charm_drift,
)


def test_calculate_gex_empty_is_zero():
    assert calculate_gex([], spot=350) == 0.0


def test_calculate_gex_call_positive_put_negative():
    call_leg = OptionLeg(strike=350, option_type="c", oi=100, iv=0.18, t_years=0.05, gamma=0.01)
    put_leg = OptionLeg(strike=350, option_type="p", oi=50, iv=0.18, t_years=0.05, gamma=0.01)
    spot = 350
    s_term = spot**2 / 100
    expected = 0.01 * 100 * 250_000 * s_term - 0.01 * 50 * 250_000 * s_term
    assert calculate_gex([call_leg, put_leg], spot) == pytest.approx(expected)


def test_find_gamma_flip_none_when_calls_only_always_positive():
    # 콜만 있으면 감마·OI·S^2 항이 전 구간에서 양수 → 부호 전환 없음
    legs = [OptionLeg(strike=350, option_type="c", oi=100, iv=0.18, t_years=0.05, gamma=0.01)]
    assert find_gamma_flip(legs, spot=350) is None


def test_find_gamma_flip_detects_sign_change():
    # put OI가 낮은 스팟 쪽에, call OI가 높은 스팟 쪽에 몰려있는 실제 시장과 유사한 구성.
    # 사전에 그리드를 스캔해 338~341 사이에서 부호가 바뀌는 것을 확인한 파라미터.
    legs = [
        OptionLeg(strike=340, option_type="p", oi=500, iv=0.18, t_years=0.05, gamma=0.0),
        OptionLeg(strike=360, option_type="c", oi=1500, iv=0.18, t_years=0.05, gamma=0.0),
    ]
    flip = find_gamma_flip(legs, spot=350)
    assert flip is not None
    assert 335 < flip < 345


def test_find_gamma_flip_does_not_leak_vollib_print_to_stdout():
    # 2026-07-08 실측: vollib.ref_python(C 확장 미설치 폴백)의 d1()이 sigma*sqrt(t)==0일 때
    # (iv=0 또는 t_years=0 — 얇거나 만기 임박 레그에서 실제로 발생) print('')을 실행해 COCKPIT
    # 하루 로그(667,663줄)의 99% 이상이 이 빈 줄이었다. iv=0.18/t_years=0.05처럼 정상적인 레그로는
    # 이 조건이 아예 트리거되지 않으므로(회귀를 못 잡는 거짓 통과), 경계 조건 레그를 써야 한다.
    legs = [OptionLeg(strike=350, option_type="c", oi=100, iv=0.0, t_years=0.0, gamma=0.0)]
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        find_gamma_flip(legs, spot=350)
    assert captured.getvalue() == ""


def test_find_gamma_flip_handles_zero_time_to_expiry_without_warning_noise(recwarn):
    # 그리드 경계에서 t_years/iv가 0에 가까우면 vollib 내부에서 0-나눗셈이 발생한다 — 계산 자체는
    # 그대로 두되(nan/inf가 부호 비교에 들어가도 flip 로직은 안전) RuntimeWarning만 억제한다.
    legs = [OptionLeg(strike=350, option_type="c", oi=100, iv=0.0, t_years=0.0, gamma=0.0)]
    find_gamma_flip(legs, spot=350)
    assert not any(issubclass(w.category, RuntimeWarning) for w in recwarn.list)


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
