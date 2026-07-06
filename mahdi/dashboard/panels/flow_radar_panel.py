"""Flow Radar 패널 — OFI 스파크라인, VPIN 독성 게이지, Microprice vs 체결가 (v6 §8, §17 COCKPIT).

VPIN 마커 색은 카테고리가 아니라 상태(status)를 나타내므로 예약된 상태 팔레트를 쓴다
(양호/경고/심각 — 시리즈 정체성 색과 절대 혼용하지 않는다).
"""

from __future__ import annotations

from datetime import datetime

import plotly.graph_objects as go

_VPIN_GOOD = "#009E73"
_VPIN_WARNING = "#E69F00"
_VPIN_CRITICAL = "#D55E00"
_VPIN_CRISIS_THRESHOLD = 0.7
_VPIN_WARNING_THRESHOLD = 0.4


def _vpin_status_color(v: float) -> str:
    if v >= _VPIN_CRISIS_THRESHOLD:
        return _VPIN_CRITICAL
    if v >= _VPIN_WARNING_THRESHOLD:
        return _VPIN_WARNING
    return _VPIN_GOOD


def build_ofi_sparkline(
    timestamps: list[datetime], ofi_series: list[float], x_range: tuple[datetime, datetime] | None = None
) -> go.Figure:
    # mode="lines"만 쓰면 점이 1개뿐인 계열(거래가 뜸한 옵션 등)은 Plotly가 선을 그릴 수 없어
    # 아무것도 안 보인다(2026-07-06 실데이터로 발견) — 마커를 항상 같이 그려 최소 1개 점은 보이게 한다.
    fig = go.Figure(
        go.Scatter(
            x=timestamps,
            y=ofi_series,
            mode="lines+markers",
            line=dict(color="#0072B2", width=2),
            marker=dict(color="#0072B2", size=5),
            hovertemplate="%{x|%H:%M}: OFI %{y:.0f}<extra></extra>",
        )
    )
    fig.add_hline(y=0, line_color="#8A8A8A", line_width=1)
    fig.update_layout(yaxis_title="OFI", showlegend=False, margin=dict(l=10, r=10, t=10, b=10), height=180)
    if x_range is not None:
        fig.update_xaxes(range=list(x_range))
    return fig


def build_vpin_chart(
    timestamps: list[datetime], vpin_series: list[float], x_range: tuple[datetime, datetime] | None = None
) -> go.Figure:
    colors = [_vpin_status_color(v) for v in vpin_series]
    fig = go.Figure(
        go.Scatter(
            x=timestamps,
            y=vpin_series,
            mode="lines+markers",
            line=dict(color="#8A8A8A", width=1),
            marker=dict(color=colors, size=6),
            hovertemplate="%{x|%H:%M}: VPIN %{y:.2f}<extra></extra>",
        )
    )
    fig.add_hline(y=_VPIN_CRISIS_THRESHOLD, line_dash="dash", line_color=_VPIN_CRITICAL, annotation_text="독성 임계(0.7)")
    fig.update_layout(
        yaxis=dict(title="VPIN", range=[0, 1]), showlegend=False, margin=dict(l=10, r=10, t=10, b=10), height=200
    )
    if x_range is not None:
        fig.update_xaxes(range=list(x_range))
    return fig


def build_microprice_vs_price_chart(
    timestamps: list[datetime],
    price_series: list[float],
    microprice_series: list[float],
    x_range: tuple[datetime, datetime] | None = None,
) -> go.Figure:
    # mode="lines"만 쓰면 점이 1개뿐인 계열(거래가 뜸한 옵션 등)은 Plotly가 선을 그릴 수 없어
    # 아무것도 안 보인다(2026-07-06 실데이터로 발견) — 마커를 항상 같이 그려 최소 1개 점은 보이게 한다.
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=timestamps, y=price_series, mode="lines+markers", name="체결가",
            line=dict(color="#8A8A8A", width=2), marker=dict(color="#8A8A8A", size=5),
        )
    )
    fig.add_trace(
        go.Scatter(
            x=timestamps, y=microprice_series, mode="lines+markers", name="Microprice",
            line=dict(color="#0072B2", width=2, dash="dot"), marker=dict(color="#0072B2", size=5),
        )
    )
    fig.update_layout(
        yaxis_title="가격", legend=dict(orientation="h", y=1.15), margin=dict(l=10, r=10, t=30, b=10), height=220
    )
    if x_range is not None:
        fig.update_xaxes(range=list(x_range))
    return fig
