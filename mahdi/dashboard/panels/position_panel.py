"""수급 패널 — 외국인/기관/개인 순매수 (v6 §10.2 Position Intelligence, §17 COCKPIT).

세 주체는 서로 다른 개체(identity)이므로 고정 카테고리 색을 쓴다.
"""

from __future__ import annotations

import plotly.graph_objects as go

_ENTITY_COLORS = {"외국인": "#0072B2", "기관": "#009E73", "개인": "#D55E00"}


def build_position_flow_chart(foreign_net: float, institution_net: float, individual_net: float) -> go.Figure:
    entities = ["외국인", "기관", "개인"]
    values = [foreign_net, institution_net, individual_net]
    colors = [_ENTITY_COLORS[e] for e in entities]

    fig = go.Figure(
        go.Bar(
            x=entities,
            y=values,
            marker_color=colors,
            text=[f"{v:+,.0f}" for v in values],
            textposition="outside",
            hovertemplate="%{x}: %{y:+,.0f}<extra></extra>",
        )
    )
    fig.add_hline(y=0, line_color="#8A8A8A", line_width=1)
    fig.update_layout(yaxis_title="순매수(억원)", showlegend=False, margin=dict(l=10, r=10, t=10, b=10), height=280)
    return fig
