"""Risk Engine — 포지션 사이징 (v6 §12.1).

Final Size = Base Size
           × Fractional Kelly (Quarter Kelly 기본, Full Kelly 절대 금지)
           × Regime Confidence
           × Signal Quality (Meta 확률)
           × Volatility Targeting (target_vol / realized_vol)
           × Liquidity Score (호가 두께·스프레드)
           × Drawdown Adjustment (DD 구간 자동 축소)
           × Portfolio Capacity Constraint (Greeks 한도 잔여분)

이 모듈은 각 팩터의 산출 로직을 갖지 않는다 — regime_confidence/signal_quality/
liquidity_score 등은 호출측(Signal Fusion/Execution Engine)이 계산해 넘긴다.
Risk Engine의 책임은 팩터를 곱해 최종 사이즈를 정하고, Full Kelly 금지·DD 자동
축소 같은 안전장치를 강제하는 것뿐이다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mahdi.config.settings import get_risk_limits


@dataclass
class PositionSizingInput:
    base_size: float
    regime_confidence: float  # 0~1
    signal_quality: float  # 0~1, 메타 모델 확률
    target_vol: float
    realized_vol: float
    liquidity_score: float  # 0~1, 호가 두께·스프레드 기반
    drawdown_pct: float  # 음수 또는 0, 예: -0.03 = 현재 -3% DD
    portfolio_capacity_remaining_pct: float  # 0~1, Greeks 한도 잔여분


@dataclass
class PositionSizingResult:
    size: float
    kelly_fraction_used: float
    volatility_targeting_factor: float
    drawdown_adjustment_factor: float
    factors: dict[str, float] = field(default_factory=dict)


def compute_position_size(
    sizing_input: PositionSizingInput,
    risk_limits: dict | None = None,
) -> PositionSizingResult:
    """
    입력: PositionSizingInput(각 팩터 0~1 정규화값 + base_size).
    계산: 8개 팩터를 전부 곱한다. kelly_fraction은 설정된 kelly_fraction과
          max_kelly_fraction 중 작은 값으로 강제 클램프(Full Kelly 절대 금지,
          v6 §12.1). Volatility Targeting = target_vol/realized_vol
          (realized_vol<=0이면 아직 변동성 추정치가 없다는 뜻이므로 중립값
          1.0을 사용). Drawdown Adjustment = 1 - min(|drawdown_pct| /
          |max_drawdown_pct|, 1.0) — DD가 한도에 가까워질수록 사이즈를 선형
          으로 줄이고, 한도 도달 시 0(v6 §12.2 "DD 구간 자동 축소").
    해석: size는 최종 계약수/명목가치 배수 — 실제 계약 수 변환은 호출측 책임.
    실패 조건: regime_confidence/signal_quality/liquidity_score/
          portfolio_capacity_remaining_pct/base_size/target_vol/realized_vol
          중 하나라도 음수면 ValueError(사이징 입력 자체가 잘못된 것 — 이런
          경우 조용히 0을 반환하면 "리스크가 없다"와 "입력이 깨졌다"를 구분할
          수 없게 되어 더 위험하다).
    """
    if risk_limits is None:
        risk_limits = get_risk_limits()

    non_negative_fields = {
        "base_size": sizing_input.base_size,
        "regime_confidence": sizing_input.regime_confidence,
        "signal_quality": sizing_input.signal_quality,
        "target_vol": sizing_input.target_vol,
        "realized_vol": sizing_input.realized_vol,
        "liquidity_score": sizing_input.liquidity_score,
        "portfolio_capacity_remaining_pct": sizing_input.portfolio_capacity_remaining_pct,
    }
    for name, value in non_negative_fields.items():
        if value < 0:
            raise ValueError(f"{name}는 음수일 수 없습니다: {value}")

    sizing_cfg = risk_limits.get("sizing", {})
    kelly_fraction = min(
        sizing_cfg.get("kelly_fraction", 0.25),
        sizing_cfg.get("max_kelly_fraction", 0.25),
    )

    vol_targeting = (
        sizing_input.target_vol / sizing_input.realized_vol
        if sizing_input.realized_vol > 0
        else 1.0
    )

    max_dd_pct = abs(risk_limits.get("limits", {}).get("max_drawdown_pct", -0.10))
    dd_adjustment = (
        1.0 - min(abs(sizing_input.drawdown_pct) / max_dd_pct, 1.0)
        if max_dd_pct > 0
        else 1.0
    )

    size = (
        sizing_input.base_size
        * kelly_fraction
        * sizing_input.regime_confidence
        * sizing_input.signal_quality
        * vol_targeting
        * sizing_input.liquidity_score
        * dd_adjustment
        * sizing_input.portfolio_capacity_remaining_pct
    )

    return PositionSizingResult(
        size=size,
        kelly_fraction_used=kelly_fraction,
        volatility_targeting_factor=vol_targeting,
        drawdown_adjustment_factor=dd_adjustment,
        factors={
            "regime_confidence": sizing_input.regime_confidence,
            "signal_quality": sizing_input.signal_quality,
            "liquidity_score": sizing_input.liquidity_score,
            "portfolio_capacity_remaining_pct": sizing_input.portfolio_capacity_remaining_pct,
            "kelly_fraction": kelly_fraction,
            "volatility_targeting": vol_targeting,
            "drawdown_adjustment": dd_adjustment,
        },
    )
