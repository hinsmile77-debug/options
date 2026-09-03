from datetime import date, datetime, timedelta

from mahdi.dashboard.panels.flow_radar_panel import (
    build_absorption_chart,
    build_cvd_chart,
    build_microprice_vs_price_chart,
    build_ofi_sparkline,
    build_vpin_chart,
    shared_x_range,
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


# ===== 2026-08-21 사용자 요청: 두 Flow Radar의 시간축 일치 + CVD 추가 =====


def test_shared_x_range_spans_both_series_not_just_the_futures_window():
    # 선물 창만 강제하면 그 밖의 옵션 봉이 보이지도 않은 채 잘려 나간다 — 합집합이어야 한다.
    futures = [datetime(2026, 8, 21, 9, 20), datetime(2026, 8, 21, 10, 15)]
    option = [datetime(2026, 8, 21, 9, 12), datetime(2026, 8, 21, 10, 30)]

    assert shared_x_range(futures, option) == (datetime(2026, 8, 21, 9, 12), datetime(2026, 8, 21, 10, 30))


def test_shared_x_range_is_none_when_there_is_nothing_to_align():
    assert shared_x_range([], []) is None
    # 점이 하나뿐이면 폭 0짜리 범위가 되어 Plotly가 축을 못 그린다 — 자동 범위에 맡긴다.
    assert shared_x_range([datetime(2026, 8, 21, 9, 20)], []) is None


def test_both_flow_radars_get_the_identical_x_axis_range():
    """회귀: 종전에는 옵션 차트에만 x_range를 넘기고 선물 차트는 자동 범위였다.

    Plotly의 자동 여백은 계열의 값 분포에 따라 달라지므로 같은 09:30이 두 그림에서 서로 다른
    가로 위치에 찍혔고, 위아래 대조가 불가능했다.
    """
    futures_ts = [datetime(2026, 8, 21, 9, 20) + timedelta(minutes=i) for i in range(3)]
    option_ts = [datetime(2026, 8, 21, 9, 21)]
    x_range = shared_x_range(futures_ts, option_ts)

    figures = [
        build_cvd_chart(futures_ts, [1.0, 2.0, 3.0], x_range=x_range),
        build_ofi_sparkline(futures_ts, [1.0, 2.0, 3.0], x_range=x_range),
        build_vpin_chart(futures_ts, [0.1, 0.2, 0.3], x_range=x_range),
        build_microprice_vs_price_chart(futures_ts, [1.0, 2.0, 3.0], [1.1, 2.1, 3.1], x_range=x_range),
        build_cvd_chart(option_ts, [5.0], x_range=x_range),
        build_ofi_sparkline(option_ts, [5.0], x_range=x_range),
        build_vpin_chart(option_ts, [0.5], x_range=x_range),
        build_microprice_vs_price_chart(option_ts, [5.0], [5.1], x_range=x_range),
    ]

    ranges = {tuple(fig.layout.xaxis.range) for fig in figures}
    assert len(ranges) == 1, "여덟 차트의 x축 범위가 하나로 같아야 위아래를 눈으로 대조할 수 있다"
    assert ranges.pop() == (futures_ts[0], futures_ts[-1])


def test_cvd_is_its_own_chart_never_a_second_axis_on_ofi():
    # OFI는 0 근처 진동, CVD는 누적 — 이중 y축으로 겹치면 두 선의 교차점이 축 스케일이 만든
    # 우연일 뿐인데 "신호"처럼 읽힌다. 두 계열은 x축만 공유하는 별도 그림이어야 한다.
    timestamps = [datetime(2026, 8, 21, 9, 20) + timedelta(minutes=i) for i in range(3)]

    ofi_fig = build_ofi_sparkline(timestamps, [1.0, -2.0, 3.0])
    cvd_fig = build_cvd_chart(timestamps, [100.0, 80.0, 260.0])

    assert len(ofi_fig.data) == 1
    assert "yaxis2" not in ofi_fig.layout.to_plotly_json()  # 이중 축을 만들지 않는다
    assert len(cvd_fig.data) == 1
    assert list(cvd_fig.data[0].y) == [100.0, 80.0, 260.0]


def test_cvd_chart_breaks_and_shades_missing_bar_gaps_like_its_siblings():
    # CVD도 미관측 구간을 직선으로 이으면 "누적이 멈춰 있었다"는 거짓 사실을 그린다.
    timestamps, _ = _gapped_series(gap_minutes=35)

    fig = build_cvd_chart(timestamps, [100.0, 220.0, 180.0, 240.0])

    assert list(fig.data[0].y) == [100.0, 220.0, None, 180.0, 240.0]
    assert len([s for s in fig.layout.shapes if s.type == "rect"]) == 1


def test_cvd_chart_shows_marker_for_single_point_series():
    fig = build_cvd_chart([datetime(2026, 8, 21, 12, 23)], [0.0])
    assert "markers" in fig.data[0].mode


# ===== 2026-08-21: Absorption을 패널에 올린다(v6 §17.3의 네 항목 중 마지막) =====


def test_absorption_chart_marks_suspect_bars_with_the_status_color():
    timestamps = [datetime(2026, 8, 21, 9, 20) + timedelta(minutes=i) for i in range(3)]

    fig = build_absorption_chart(timestamps, [0.0, 1.2, 3.4])

    colors = list(fig.data[0].marker.color)
    assert colors[:2] == ["#6E7B1F", "#6E7B1F"]
    assert colors[2] == "#D55E00", "3배를 넘은 봉만 심각색 — 임계 초과라는 같은 뜻으로만 빌린다"


def test_absorption_chart_distinguishes_judged_zero_from_no_baseline():
    """0(판정했고 흡수 아님)과 None(기준선 없어 판정 못 함)은 **다르게 그려야 한다.**

    높이 0인 막대는 안 보이므로 막대만으로는 둘이 구분되지 않는다 — 08-05(미관측을 정상으로
    표시)·08-21(모르는 것을 매수로 분류)에서 두 번 겪은 실수의 같은 형태다.
    """
    timestamps = [datetime(2026, 8, 21, 9, 20) + timedelta(minutes=i) for i in range(4)]

    fig = build_absorption_chart(timestamps, [None, None, 0.0, 2.0])

    # 판정한 봉만 막대·점을 갖는다.
    assert list(fig.data[0].x) == timestamps[2:]
    assert list(fig.data[1].x) == timestamps[2:]  # y=0 관측 점
    assert list(fig.data[1].y) == [0, 0]
    # 판정 못 한 앞머리는 음영 + 라벨로 명시된다.
    bands = [s for s in fig.layout.shapes if s.type == "rect"]
    assert len(bands) == 1
    assert (bands[0].x0, bands[0].x1) == (timestamps[0], timestamps[1])
    assert any("기준선 없음" in (a.text or "") for a in fig.layout.annotations)


def test_absorption_threshold_line_stays_visible_on_a_quiet_day():
    # 값이 전부 작아도 3배 선이 화면 밖으로 나가면 「임계에 얼마나 가까운가」를 못 읽는다.
    timestamps = [datetime(2026, 8, 21, 9, 20) + timedelta(minutes=i) for i in range(3)]

    fig = build_absorption_chart(timestamps, [0.0, 0.3, 0.9])

    assert fig.layout.yaxis.range[1] >= 3.0


def test_absorption_chart_is_bars_not_a_line():
    # 봉마다 독립인 사건 크기다 — 선으로 이으면 0이 이어진 구간이 평탄한 신호처럼 보인다.
    timestamps = [datetime(2026, 8, 21, 9, 20) + timedelta(minutes=i) for i in range(2)]

    fig = build_absorption_chart(timestamps, [0.0, 1.0])

    assert fig.data[0].type == "bar"


def test_absorption_chart_is_safe_when_nothing_can_be_judged_yet():
    timestamps = [datetime(2026, 8, 21, 9, 20) + timedelta(minutes=i) for i in range(3)]

    fig = build_absorption_chart(timestamps, [None, None, None])

    assert list(fig.data[0].y) == []
    assert fig.layout.yaxis.range[1] >= 3.0


def _vline_labels(fig) -> list[str]:
    return [a.text for a in fig.layout.annotations if a.text]


def test_gamma_profile_chart_stamps_both_price_lines_with_their_observation_time():
    """2026-09-03 회귀 — 08-05가 지수선과 선물선을 갈라놓고도 **어느 쪽도 몇 시 값인지 안 적었다.**

    09-03 화면: 지수 1,017.56(14:30) · 선물 1,030.45(15:34). 13p가 벌어져 있는데 시각이 없으니
    그것이 베이시스인지 지연인지 화면에서 답할 수 없었다 — 08-05가 «창 이동 지연인가 두 가격의
    차이인가» 때문에 선을 나눈 것과 같은 물음이 시간 축에 그대로 남아 있었다.
    """
    fig = build_gamma_profile_chart(
        [1030.0], [10.0], spot=1017.56, gamma_flip=None, gamma_walls=[],
        expiry=date(2026, 9, 10), futures_price=1030.45,
        spot_asof=datetime(2026, 9, 3, 14, 30), spot_is_stale=True,
        futures_asof=datetime(2026, 9, 3, 15, 34),
    )

    labels = _vline_labels(fig)
    assert any("14:30" in t for t in labels)
    assert any("15:34" in t for t in labels)


def test_gamma_profile_chart_marks_a_stale_index_line_instead_of_calling_it_current():
    """회색 점선 + "현재가"는 그 자체로 «지금 값»이라는 주장이다. 낡았으면 그렇게 말해야 한다."""
    stale = build_gamma_profile_chart(
        [1030.0], [10.0], spot=1017.56, gamma_flip=None, gamma_walls=[],
        spot_asof=datetime(2026, 9, 3, 14, 30), spot_is_stale=True,
    )
    fresh = build_gamma_profile_chart(
        [1030.0], [10.0], spot=1030.1, gamma_flip=None, gamma_walls=[],
        spot_asof=datetime(2026, 9, 3, 14, 30), spot_is_stale=False,
    )

    assert any("낡음" in t for t in _vline_labels(stale))
    assert not any("낡음" in t for t in _vline_labels(fresh))
    assert any("현재가" in t for t in _vline_labels(fresh))
    # 선은 지운 것이 아니라 다르게 그린다 — 스팟이 행사가 창의 어디에 있는지가 곧 사고의 크기다.
    stale_line = [s for s in stale.layout.shapes if s.x0 == s.x1 == 1017.56]
    assert len(stale_line) == 1
    assert stale_line[0].line.color != fresh.layout.shapes[0].line.color


def test_gamma_profile_chart_omits_the_clock_when_the_observation_time_is_unknown():
    """시각을 모르면 지어내지 않는다 — 합성 폴백·구버전 행에서 `None`이 온다."""
    fig = build_gamma_profile_chart(
        [1030.0], [10.0], spot=1030.0, gamma_flip=None, gamma_walls=[], futures_price=1030.2
    )

    assert "지수 현재가" in _vline_labels(fig)
    assert "선물(행사가 창 기준)" in _vline_labels(fig)
