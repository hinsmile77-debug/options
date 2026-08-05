from datetime import date, datetime, timedelta

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


# ===== 2026-08-05 P1-7: 확률 막대와 WARMUP 상수를 같게 그리지 않는다 =====


def test_regime_chart_marks_warmup_fallback_as_not_a_probability():
    """08-05 화면의 "평균회귀 100%"는 확신이 아니라 **확률을 계산한 적이 없다**는 뜻이었다.

    `RegimeEngine.fit()`이 한 번도 실행된 적 없어(feature_store 6,830/8,000행)
    `warmup_fallback()`이 전일 마감 레짐에 1.0을 박은 one-hot 상수를 냈다.
    """
    prob = {r: 0.0 for r in RegimeLabel}
    prob[RegimeLabel.RANGE_BALANCED] = 1.0

    fig = build_regime_probability_chart(prob, is_warmup=True)

    assert "WARMUP" in fig.layout.title.text


def test_regime_chart_says_trained_when_not_warmup():
    prob = {r: 0.125 for r in RegimeLabel}
    fig = build_regime_probability_chart(prob, is_warmup=False)
    assert "사후확률" in fig.layout.title.text
    assert "WARMUP" not in fig.layout.title.text


def test_regime_chart_says_unknown_for_rows_predating_the_migration():
    # 학습된 판정으로 가정하는 쪽이 더 위험하다 — 모르면 모른다고 쓴다.
    fig = build_regime_probability_chart({r: 0.125 for r in RegimeLabel}, is_warmup=None)
    assert "구분 불가" in fig.layout.title.text


def test_regime_chart_keeps_the_hundred_percent_label_visible():
    """08-05 화면에서 **유일하게 의미 있던 100% 막대만** 값 라벨이 안 보였다 —
    textposition="outside"인 라벨이 x축 범위(0~1) 밖으로 잘려나갔기 때문이다."""
    prob = {r: 0.0 for r in RegimeLabel}
    prob[RegimeLabel.RANGE_BALANCED] = 1.0

    fig = build_regime_probability_chart(prob, is_warmup=True)

    assert fig.data[0].cliponaxis is False
    assert fig.layout.xaxis.range[1] > 1.0  # 라벨이 들어갈 여백


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


# ===== 2026-08-05 P0-1: 봉이 없는 구간을 직선으로 잇지 않는다 =====
#
# 08-05 실측: 옵션 B09F9WA21이 11:27:02 구독 해제 → 12:02:01 재구독(ATM 창 이탈) 되면서 그
# 35분간 봉이 하나도 없었는데, 세 차트 모두 그 구간을 직선으로 그려 "조용한 시장"으로 보이게
# 했다. VPIN 평탄선이 특히 위험하다 — "독성 낮음 유지"로 읽힌다.


def _gapped_series(gap_minutes: int) -> tuple[list[datetime], list[float]]:
    """11:25·11:26 두 봉 뒤 gap_minutes 공백, 그 뒤 두 봉."""
    base = datetime(2026, 8, 5, 11, 25)
    timestamps = [
        base,
        base + timedelta(minutes=1),
        base + timedelta(minutes=1 + gap_minutes),
        base + timedelta(minutes=2 + gap_minutes),
    ]
    return timestamps, [1.0, 2.0, 3.0, 4.0]


def test_ofi_sparkline_breaks_the_line_across_a_missing_bar_gap():
    timestamps, ofi = _gapped_series(gap_minutes=35)

    fig = build_ofi_sparkline(timestamps, ofi)

    y = list(fig.data[0].y)
    assert None in y, "봉이 없는 구간은 선이 끊겨야 한다 — 이어 그리면 미관측이 정상으로 보인다"
    assert y == [1.0, 2.0, None, 3.0, 4.0]
    # 끊김 점의 x는 공백의 중간 — 양 끝에 붙이면 실제 봉의 마커와 겹친다.
    assert list(fig.data[0].x)[2] == datetime(2026, 8, 5, 11, 43, 30)


def test_ofi_sparkline_keeps_consecutive_bars_connected():
    timestamps = [datetime(2026, 8, 5, 11, 25) + timedelta(minutes=i) for i in range(4)]

    fig = build_ofi_sparkline(timestamps, [1.0, 2.0, 3.0, 4.0])

    assert list(fig.data[0].y) == [1.0, 2.0, 3.0, 4.0]  # 정상 1분 간격엔 끊김 점이 없다


def test_vpin_chart_breaks_gaps_and_keeps_marker_colors_aligned():
    timestamps, _ = _gapped_series(gap_minutes=35)
    vpin = [0.1, 0.5, 0.8, 0.5]  # good, warning, critical, warning

    fig = build_vpin_chart(timestamps, vpin)

    assert list(fig.data[0].y) == [0.1, 0.5, None, 0.8, 0.5]
    colors = list(fig.data[0].marker.color)
    # 색은 끊김 점이 끼워진 뒤의 계열 기준 — 원본 기준으로 매기면 한 칸씩 밀려 0.8이 경고색이 된다.
    assert colors[0] == "#009E73"
    assert colors[1] == "#E69F00"
    assert colors[3] == "#D55E00"
    assert colors[4] == "#E69F00"


def test_microprice_chart_breaks_both_series_at_the_same_gap():
    timestamps, _ = _gapped_series(gap_minutes=35)

    fig = build_microprice_vs_price_chart(timestamps, [10.0, 11.0, 12.0, 13.0], [10.1, 11.1, 12.1, 13.1])

    assert list(fig.data[0].y) == [10.0, 11.0, None, 12.0, 13.0]
    assert list(fig.data[1].y) == [10.1, 11.1, None, 12.1, 13.1]


def test_charts_shade_long_gaps_with_a_labeled_band():
    timestamps, ofi = _gapped_series(gap_minutes=35)

    fig = build_ofi_sparkline(timestamps, ofi)

    bands = [s for s in fig.layout.shapes if s.type == "rect"]
    assert len(bands) == 1
    assert bands[0].x0 == datetime(2026, 8, 5, 11, 26)
    assert bands[0].x1 == datetime(2026, 8, 5, 12, 1)
    assert any("봉 없음" in (a.text or "") for a in fig.layout.annotations)


def test_short_gaps_break_the_line_but_are_not_shaded():
    # 거래가 얇은 옵션은 1~2분 공백이 흔하다 — 그것까지 칠하면 화면이 음영으로 뒤덮여
    # 정작 긴 공백(결손)이 안 보인다. 결손 임계(5분)는 헬스체크 배지와 같은 기준을 쓴다.
    timestamps, ofi = _gapped_series(gap_minutes=3)

    fig = build_ofi_sparkline(timestamps, ofi)

    assert None in list(fig.data[0].y)  # 선은 끊는다
    assert [s for s in fig.layout.shapes if s.type == "rect"] == []  # 음영은 안 칠한다


def test_overnight_gap_is_not_shaded_as_missing_bars():
    # 야간/주말 공백은 결손이 아니라 정상이고 rangebreaks가 이미 x축에서 접는다 —
    # 칠하면 매일 아침 상시 오경보가 된다(CB 하트비트에서 배운 실수).
    timestamps = [
        datetime(2026, 8, 4, 15, 44),
        datetime(2026, 8, 4, 15, 45),
        datetime(2026, 8, 5, 9, 0),
        datetime(2026, 8, 5, 9, 1),
    ]

    fig = build_ofi_sparkline(timestamps, [1.0, 2.0, 3.0, 4.0])

    assert None in list(fig.data[0].y)  # 어제 종가와 오늘 시가를 잇지는 않는다
    assert [s for s in fig.layout.shapes if s.type == "rect"] == []


def test_gap_handling_is_safe_for_single_point_series():
    fig = build_ofi_sparkline([datetime(2026, 8, 5, 12, 23)], [0.0])
    assert list(fig.data[0].y) == [0.0]
    assert [s for s in fig.layout.shapes if s.type == "rect"] == []


# ===== 2026-08-05 P0-2: Gamma Map은 어느 북인지 밝힌다 =====


def test_gamma_profile_chart_states_which_expiry_book_it_shows():
    fig = build_gamma_profile_chart(
        [1045.0], [10.0], spot=1045.0, gamma_flip=None, gamma_walls=[], expiry=date(2026, 8, 13)
    )
    assert "2026-08-13" in fig.layout.title.text


def test_gamma_profile_chart_says_unknown_expiry_instead_of_omitting_it():
    # 만기를 생략하면 "전 만기 합산"으로 오독된다 — 08-05 화면이 정확히 그 상태였다.
    fig = build_gamma_profile_chart([], [], spot=1045.0, gamma_flip=None, gamma_walls=[], expiry=None)
    assert "만기 미상" in fig.layout.title.text


def test_gamma_profile_chart_draws_the_futures_line_that_moves_the_strike_window():
    """2026-08-05 P1-6 — 행사가 창은 **선물 체결가**로 굴러가는데 차트의 "현재가"는 지수였다.

    08-05 실측에서 둘이 3p 넘게 벌어져 창이 스팟 위로 치우쳐 보였는데, 선이 하나뿐이라 그것이
    창 이동 지연인지 두 가격의 차이인지 구분할 수 없었다.
    """
    fig = build_gamma_profile_chart(
        [1040.0, 1045.0, 1050.0], [1.0, 2.0, 3.0],
        spot=1042.85, gamma_flip=None, gamma_walls=[], expiry=date(2026, 8, 13),
        futures_price=1046.30,
    )

    labels = [a.text for a in fig.layout.annotations]
    assert "지수 현재가" in labels
    assert "선물(행사가 창 기준)" in labels
    xs = {s.x0 for s in fig.layout.shapes if s.type == "line"}
    assert {1042.85, 1046.30} <= xs


def test_gamma_profile_chart_omits_the_futures_line_when_unknown():
    fig = build_gamma_profile_chart(
        [1045.0], [1.0], spot=1045.0, gamma_flip=None, gamma_walls=[], expiry=None, futures_price=None
    )
    assert "선물(행사가 창 기준)" not in [a.text for a in fig.layout.annotations]


def test_gamma_profile_chart_labels_a_single_wall_by_name_not_rank():
    fig = build_gamma_profile_chart(
        [1045.0], [10.0], spot=1045.0, gamma_flip=None, gamma_walls=[1045.0], expiry=date(2026, 8, 13)
    )
    assert any((a.text or "") == "감마월" for a in fig.layout.annotations)
