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
