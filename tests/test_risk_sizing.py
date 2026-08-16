import pytest

from mahdi.config.settings import get_risk_limits
from mahdi.risk.sizing import PositionSizingInput, compute_position_size

_RISK_LIMITS = {
    "sizing": {"kelly_fraction": 0.25, "max_kelly_fraction": 0.25},
    "limits": {"max_drawdown_pct": -0.10},
}


def _input(**overrides) -> PositionSizingInput:
    base = dict(
        base_size=100.0,
        regime_confidence=1.0,
        signal_quality=1.0,
        target_vol=0.01,
        realized_vol=0.01,
        liquidity_score=1.0,
        drawdown_pct=0.0,
        portfolio_capacity_remaining_pct=1.0,
    )
    base.update(overrides)
    return PositionSizingInput(**base)


def test_all_neutral_factors_yields_kelly_fraction_of_base_size():
    result = compute_position_size(_input(), risk_limits=_RISK_LIMITS)
    # 모든 팩터가 1.0(중립)이면 최종 사이즈 = base_size × kelly_fraction
    assert result.size == pytest.approx(100.0 * 0.25)
    assert result.kelly_fraction_used == 0.25


def test_kelly_fraction_clamped_to_max_even_if_config_kelly_larger():
    risk_limits = {
        "sizing": {"kelly_fraction": 0.9, "max_kelly_fraction": 0.25},
        "limits": {"max_drawdown_pct": -0.10},
    }
    result = compute_position_size(_input(), risk_limits=risk_limits)
    # Full Kelly 절대 금지 — kelly_fraction이 max_kelly_fraction을 넘지 않는다
    assert result.kelly_fraction_used == 0.25


def test_volatility_targeting_scales_size_up_when_realized_vol_low():
    result = compute_position_size(
        _input(target_vol=0.02, realized_vol=0.01), risk_limits=_RISK_LIMITS
    )
    assert result.volatility_targeting_factor == pytest.approx(2.0)


def test_volatility_targeting_falls_back_to_neutral_when_realized_vol_zero():
    result = compute_position_size(
        _input(target_vol=0.02, realized_vol=0.0), risk_limits=_RISK_LIMITS
    )
    assert result.volatility_targeting_factor == 1.0


def test_drawdown_adjustment_linearly_shrinks_size():
    # max_drawdown_pct = -0.10, drawdown_pct = -0.05 → 절반 지점, 조정계수 0.5
    result = compute_position_size(
        _input(drawdown_pct=-0.05), risk_limits=_RISK_LIMITS
    )
    assert result.drawdown_adjustment_factor == pytest.approx(0.5)
    assert result.size == pytest.approx(100.0 * 0.25 * 0.5)


def test_drawdown_at_limit_zeroes_out_size():
    result = compute_position_size(
        _input(drawdown_pct=-0.10), risk_limits=_RISK_LIMITS
    )
    assert result.drawdown_adjustment_factor == 0.0
    assert result.size == 0.0


def test_drawdown_beyond_limit_does_not_go_negative():
    result = compute_position_size(
        _input(drawdown_pct=-0.50), risk_limits=_RISK_LIMITS
    )
    assert result.drawdown_adjustment_factor == 0.0
    assert result.size == 0.0


@pytest.mark.parametrize(
    "field_name",
    [
        "base_size",
        "regime_confidence",
        "signal_quality",
        "target_vol",
        "realized_vol",
        "liquidity_score",
        "portfolio_capacity_remaining_pct",
    ],
)
def test_negative_factor_raises_value_error(field_name):
    with pytest.raises(ValueError):
        compute_position_size(_input(**{field_name: -1.0}), risk_limits=_RISK_LIMITS)


def test_default_risk_limits_loaded_from_yaml_when_not_provided():
    # risk_limits=None이면 실제 mahdi/config/risk_limits.yaml을 로드해야 한다
    real_limits = get_risk_limits()
    result = compute_position_size(_input())
    assert result.kelly_fraction_used == min(
        real_limits["sizing"]["kelly_fraction"],
        real_limits["sizing"]["max_kelly_fraction"],
    )


# ===== 2026-08-16 (Block D) — 정수 계약수 변환 =====


def test_contracts_from_size_floors_rather_than_rounds_up():
    """올림은 사이징이 계산한 위험을 **초과하는** 쪽이다 — 0.9를 1로 만들면 그 11%는 근거가 없다."""
    from mahdi.risk.sizing import contracts_from_size

    limits = {"sizing": {}}
    assert contracts_from_size(0.9, limits) == 0
    assert contracts_from_size(1.0, limits) == 1
    assert contracts_from_size(2.7, limits) == 2


def test_contracts_from_size_applies_the_fixed_lever():
    """개시 주간은 `fixed_contracts: 1`로 규모를 묶는다 — Kelly 입력 4종이 전부 중립값이라
    근거 없는 팩터의 곱을 계약수로 바꾸는 것보다 방향성만 먼저 검증하는 것이 맞다."""
    from mahdi.risk.sizing import contracts_from_size

    assert contracts_from_size(0.25, {"sizing": {"fixed_contracts": 1}}) == 1
    assert contracts_from_size(7.9, {"sizing": {"fixed_contracts": 1}}) == 1
    assert contracts_from_size(0.25, {"sizing": {"fixed_contracts": 3}}) == 3


def test_fixed_contracts_never_overrides_a_zero_size():
    """**이 테스트가 D-1의 핵심이다.**

    `size == 0`은 우연이 아니라 판정이다 — Drawdown Adjustment는 한도 도달 시 0을 내고
    (v6 §12.2), 팩터 하나가 0이면 곱이 0이 된다. 고정 계약수가 그것을 1로 만들면
    **리스크 한도를 설정 한 줄로 우회**하게 된다.
    """
    from mahdi.risk.sizing import contracts_from_size

    assert contracts_from_size(0.0, {"sizing": {"fixed_contracts": 1}}) == 0
    assert contracts_from_size(-1.0, {"sizing": {"fixed_contracts": 5}}) == 0


def test_the_shipped_config_fixes_one_contract_for_the_opening_week():
    """설정 파일이 실제로 1로 묶여 있는지 — 주석만 있고 값이 없으면 아무 일도 안 한다."""
    from mahdi.config.settings import get_risk_limits

    assert (get_risk_limits().get("sizing") or {}).get("fixed_contracts") == 1
