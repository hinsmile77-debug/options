"""Regime 패널 — 현재 레짐 배지 + 8-state 확률 막대 (v6 §17 COCKPIT, PART 7).

색상은 Okabe-Ito CVD-safe 팔레트를 RegimeLabel 고정 순서에 매핑한다(카테고리 색상은
순위가 아니라 정체성을 따라야 하므로, 확률 크기와 무관하게 레짐마다 항상 같은 색).
"""

from __future__ import annotations

import plotly.graph_objects as go

from mahdi.engines.regime import RegimeLabel

_REGIME_COLORS: dict[RegimeLabel, str] = {
    RegimeLabel.TREND_UP_STRONG: "#0072B2",
    RegimeLabel.TREND_DOWN_STRONG: "#D55E00",
    RegimeLabel.RANGE_BALANCED: "#009E73",
    RegimeLabel.RANGE_BREAK_PREP: "#E69F00",
    RegimeLabel.VOL_EXPANSION: "#CC79A7",
    RegimeLabel.VOL_COMPRESSION: "#56B4E9",
    RegimeLabel.LIQUIDITY_THIN: "#F0E442",
    RegimeLabel.CRISIS_DEFENSE: "#000000",
}

REGIME_LABEL_KO: dict[RegimeLabel, str] = {
    RegimeLabel.TREND_UP_STRONG: "강한 상승 추세",
    RegimeLabel.TREND_DOWN_STRONG: "강한 하락 추세",
    RegimeLabel.RANGE_BALANCED: "평균회귀",
    RegimeLabel.RANGE_BREAK_PREP: "압축·돌파대기",
    RegimeLabel.VOL_EXPANSION: "변동성 팽창",
    RegimeLabel.VOL_COMPRESSION: "변동성 압축",
    RegimeLabel.LIQUIDITY_THIN: "유동성 빈약",
    RegimeLabel.CRISIS_DEFENSE: "위기·이벤트",
}


def build_regime_probability_chart(regime_prob: dict[RegimeLabel, float]) -> go.Figure:
    """8개 레짐의 현재 확률을 가로 막대로 표시 (레짐당 고정 색상, 범례 불필요 — 축 라벨이 정체성을 대신함)."""
    labels = list(RegimeLabel)
    values = [regime_prob.get(r, 0.0) for r in labels]
    colors = [_REGIME_COLORS[r] for r in labels]
    names = [REGIME_LABEL_KO[r] for r in labels]

    fig = go.Figure(
        go.Bar(
            x=values,
            y=names,
            orientation="h",
            marker_color=colors,
            text=[f"{v:.0%}" for v in values],
            textposition="outside",
            hovertemplate="%{y}: %{x:.1%}<extra></extra>",
        )
    )
    fig.update_layout(
        xaxis=dict(title="확률", range=[0, 1], tickformat=".0%"),
        yaxis=dict(title=None, autorange="reversed"),
        showlegend=False,
        margin=dict(l=10, r=10, t=10, b=10),
        height=280,
    )
    return fig
