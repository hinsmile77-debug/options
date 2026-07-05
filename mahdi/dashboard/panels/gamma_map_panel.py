"""Gamma Map 패널 — 행사가별 GEX 프로파일 + Gamma Flip/Wall 표시 (v6 §9, §17 COCKPIT).

GEX 부호는 극성(polarity) 정보이므로 다이버징 두 색 + 중립을 사용한다: 양(+, 딜러 안정화)은
파랑, 음(-, 증폭)은 버밀리언 — 무지개색이나 임의 카테고리 색을 쓰지 않는다.
"""

from __future__ import annotations

import plotly.graph_objects as go

_NEG_GEX_COLOR = "#D55E00"  # 음(-) GEX — 변동성 증폭
_POS_GEX_COLOR = "#0072B2"  # 양(+) GEX — 변동성 억제
_NEUTRAL_COLOR = "#8A8A8A"
_FLIP_COLOR = "#CC79A7"
_WALL_COLOR = "#E69F00"


def build_gamma_profile_chart(
    strikes: list[float],
    gex_by_strike: list[float],
    spot: float,
    gamma_flip: float | None,
    gamma_walls: list[float],
) -> go.Figure:
    colors = [_POS_GEX_COLOR if g >= 0 else _NEG_GEX_COLOR for g in gex_by_strike]

    fig = go.Figure(
        go.Bar(
            x=strikes,
            y=gex_by_strike,
            marker_color=colors,
            hovertemplate="행사가 %{x}: GEX %{y:,.0f}<extra></extra>",
        )
    )
    fig.add_vline(x=spot, line_dash="dot", line_color=_NEUTRAL_COLOR, annotation_text="현재가")
    if gamma_flip is not None:
        fig.add_vline(x=gamma_flip, line_dash="dash", line_color=_FLIP_COLOR, annotation_text="Gamma Flip")
    for wall in gamma_walls:
        fig.add_vline(x=wall, line_color=_WALL_COLOR, opacity=0.4)

    fig.update_layout(
        xaxis_title="행사가",
        yaxis_title="GEX",
        showlegend=False,
        margin=dict(l=10, r=10, t=30, b=10),
        height=320,
    )
    return fig
