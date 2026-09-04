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
    # 2026-09-04 단위 확정 — KIS 응답(*_ntby_tr_pbmn)의 단위는 **백만원**이다. HTS[7221]
    # "투자자별 매매종합"(선물=계약, 옵션=억원) 12:05 스냅샷을 KOSPI200 선물 종가(A01609
    # 1050.10) × 거래승수 25만원으로 환산해 대조한 결과, 외국인 +0.003%/기관 +0.09%/개인
    # -1.4%(개인은 순계약 270으로 작아 평균체결가 차이가 크게 보이는 것)로 일치했다.
    # 3주체 합이 0이 아닌 것은 브로커의 "기타법인"(당시 +22계약)을 적재하지 않기 때문이다.
    fig.update_layout(
        yaxis_title="순매수대금 (백만원)", showlegend=False, margin=dict(l=10, r=10, t=10, b=10), height=280
    )
    return fig
