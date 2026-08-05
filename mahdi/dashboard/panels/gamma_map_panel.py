"""Gamma Map 패널 — 행사가별 GEX 프로파일 + Gamma Flip/Wall 표시 (v6 §9, §17 COCKPIT).

GEX 부호는 극성(polarity) 정보이므로 다이버징 두 색 + 중립을 사용한다: 양(+, 딜러 안정화)은
파랑, 음(-, 증폭)은 버밀리언 — 무지개색이나 임의 카테고리 색을 쓰지 않는다.

2026-08-05(P0-2): **어느 만기(북)의 감마 맵인지 제목에 반드시 쓴다.** 값 자체는 이미 먼슬리 한
북으로 좁혀졌지만(`data_source.signal_book_legs()`), 화면에 그 사실이 없으면 사용자는 여전히
"세 북 전부"로 읽는다 — 08-05 화면이 정확히 그 상태였다.
"""

from __future__ import annotations

from datetime import date

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
    expiry: date | None = None,
) -> go.Figure:
    """
    입력: 행사가/행사가별 GEX/스팟/감마플립/감마월 + `expiry`(이 값들이 나온 북의 만기).
    계산: GEX 막대(부호로 색 구분) + 현재가·감마플립·감마월 수직선. 제목에 만기를 명시한다.
    해석: `expiry=None`은 "체인이 비어 어느 북인지도 특정 못 함"이다 — 그 경우 제목에
         **"만기 미상"** 을 쓴다(만기를 생략해 "전 만기 합산"으로 오독되게 두지 않는다).
    """
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
    # gamma_walls는 |Gamma x OI| 노출 내림차순(1번이 가장 강한 Pinning 후보). 2026-08-05(P0-3)부터
    # 엔진과 같이 **1개만** 오고(노출 0이면 아예 안 온다), 그래서 순위 번호 대신 이름을 붙인다 —
    # 종전의 "GW1/GW2/GW3"는 행사가 창이 5개뿐인 상황에서 창의 양 끝을 가리키는 것에 가까웠다.
    # 여러 개가 오더라도 깨지지 않게 순위 표기는 2번째부터만 붙인다.
    _WALL_ANNOTATION_POSITIONS = ["top", "top left", "top right"]
    for i, wall in enumerate(gamma_walls):
        fig.add_vline(
            x=wall,
            line_color=_WALL_COLOR,
            opacity=0.4,
            annotation_text="감마월" if i == 0 else f"감마월{i + 1}",
            annotation_position=_WALL_ANNOTATION_POSITIONS[i % len(_WALL_ANNOTATION_POSITIONS)],
        )

    title = f"만기 {expiry.isoformat()} 한 북" if expiry is not None else "만기 미상(체인 없음)"
    fig.update_layout(
        title=dict(text=title, font=dict(size=12), x=0, xanchor="left"),
        xaxis_title="행사가",
        yaxis_title="GEX",
        showlegend=False,
        margin=dict(l=10, r=10, t=36, b=10),
        height=320,
    )
    return fig
