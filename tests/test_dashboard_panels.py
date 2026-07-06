from datetime import datetime, timedelta

from mahdi.dashboard.panels.flow_radar_panel import (
    build_microprice_vs_price_chart,
    build_ofi_sparkline,
    build_vpin_chart,
)
from mahdi.dashboard.panels.gamma_map_panel import build_gamma_profile_chart
from mahdi.dashboard.panels.position_panel import build_position_flow_chart
from mahdi.dashboard.panels.regime_panel import build_regime_probability_chart
from mahdi.engines.regime import RegimeLabel


def test_regime_probability_chart_has_one_bar_per_regime_with_fixed_colors():
    prob = {r: 0.0 for r in RegimeLabel}
    prob[RegimeLabel.VOL_EXPANSION] = 1.0

    fig = build_regime_probability_chart(prob)

    bar = fig.data[0]
    assert len(bar.x) == len(RegimeLabel)
    # VOL_EXPANSION은 RegimeLabel 순서상 5번째(index 4)
    assert bar.x[4] == 1.0
    assert bar.marker.color[4] == "#CC79A7"


def test_gamma_profile_chart_colors_by_sign_not_magnitude():
    strikes = [345, 350, 355]
    gex = [-100.0, 50.0, -20.0]

    fig = build_gamma_profile_chart(strikes, gex, spot=350, gamma_flip=349.0, gamma_walls=[345])

    colors = fig.data[0].marker.color
    assert colors[0] == "#D55E00"  # 음수
    assert colors[1] == "#0072B2"  # 양수
    assert colors[2] == "#D55E00"  # 음수


def test_gamma_profile_chart_handles_no_flip_or_walls():
    fig = build_gamma_profile_chart([350], [10.0], spot=350, gamma_flip=None, gamma_walls=[])
    assert fig.data[0].y[0] == 10.0


def test_ofi_sparkline_plots_full_series():
    timestamps = [datetime(2026, 7, 5, 9, i) for i in range(5)]
    ofi = [10.0, -5.0, 20.0, 0.0, -15.0]

    fig = build_ofi_sparkline(timestamps, ofi)

    assert list(fig.data[0].y) == ofi


def test_ofi_sparkline_applies_explicit_x_range_when_given():
    # 2026-07-06: 데이터가 1~2개뿐인 계열(예: 얇은 옵션)은 Plotly가 그 점 주위로만 확대해
    # x축이 마이크로초 단위로 깨진다 — 다른 계열(선물)과 같은 범위를 강제로 맞출 수 있어야 한다.
    timestamps = [datetime(2026, 7, 5, 9, 30)]
    fig = build_ofi_sparkline(timestamps, [5.0], x_range=(datetime(2026, 7, 5, 9, 0), datetime(2026, 7, 5, 10, 0)))

    assert list(fig.layout.xaxis.range) == [datetime(2026, 7, 5, 9, 0), datetime(2026, 7, 5, 10, 0)]


def test_ofi_sparkline_shows_marker_for_single_point_series():
    # mode="lines"만 쓰면 점이 1개뿐일 때 Plotly가 선을 못 그려 아무것도 안 보인다
    # (2026-07-06 거래가 뜸한 옵션 실데이터로 발견) — 마커가 항상 같이 그려져야 한다.
    fig = build_ofi_sparkline([datetime(2026, 7, 6, 12, 23)], [0.0])
    assert "markers" in fig.data[0].mode


def test_microprice_vs_price_chart_shows_markers_for_single_point_series():
    fig = build_microprice_vs_price_chart([datetime(2026, 7, 6, 12, 23)], [49.6], [49.55])
    assert all("markers" in trace.mode for trace in fig.data)


def test_vpin_chart_marks_status_colors_by_threshold():
    timestamps = [datetime(2026, 7, 5, 9, i) for i in range(3)]
    vpin = [0.1, 0.5, 0.8]  # good, warning, critical

    fig = build_vpin_chart(timestamps, vpin)

    colors = fig.data[0].marker.color
    assert colors[0] == "#009E73"
    assert colors[1] == "#E69F00"
    assert colors[2] == "#D55E00"


def test_microprice_vs_price_chart_has_two_named_series():
    timestamps = [datetime(2026, 7, 5, 9, 0) + timedelta(minutes=i) for i in range(3)]
    price = [350.0, 350.5, 350.2]
    micro = [350.1, 350.4, 350.3]

    fig = build_microprice_vs_price_chart(timestamps, price, micro)

    names = {trace.name for trace in fig.data}
    assert names == {"체결가", "Microprice"}


def test_position_flow_chart_signed_values_and_colors():
    fig = build_position_flow_chart(foreign_net=500.0, institution_net=-200.0, individual_net=-300.0)

    bar = fig.data[0]
    assert list(bar.y) == [500.0, -200.0, -300.0]
    assert bar.marker.color[0] == "#0072B2"
