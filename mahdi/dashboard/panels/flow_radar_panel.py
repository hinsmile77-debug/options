"""Flow Radar 패널 — CVD, OFI, VPIN 독성 게이지, Absorption, Microprice vs 체결가 (v6 §8, §17 COCKPIT).

VPIN 마커 색은 카테고리가 아니라 상태(status)를 나타내므로 예약된 상태 팔레트를 쓴다
(양호/경고/심각 — 시리즈 정체성 색과 절대 혼용하지 않는다).

2026-08-05(COCKPIT 육안 점검 P0-1) — **봉이 없는 구간을 직선으로 잇지 않는다.**

08-05 화면에서 옵션(B09F9WA21)의 OFI/VPIN/가격 세 차트가 모두 11:26~12:02를 직선으로 그렸다.
그 35분은 시장이 조용했던 게 아니라 **마흐디가 그 종목을 안 보고 있던 시간**이다(관측 루프 로그:
11:27:02 `WS 구독 해제` → 12:02:01 `WS 구독 요청` — 선물이 흔들리며 ATM±2 창이 그 종목을
벗어났다가 돌아왔다). 그런데 화면은 VPIN 0.47 평탄선을 그려 "독성 낮은 상태 유지"로 읽히게
만들었다. 이 프로젝트가 반복해 지켜온 원칙("정상을 이상으로 표시하면 진짜 이상을 못 알아본다",
`_CLOSING_AUCTION_START` 주석)의 **정확한 역방향** — 미관측을 정상으로 표시하고 있었다.

`market_raw_1m`만으로는 "체결이 없었다"와 "구독에서 빠져 있었다"를 구분할 수 없으므로, 화면은
구분되는 척하지 않고 **"봉 없음"** 이라고만 쓴다(둘 중 무엇인지는 관측 루프 로그가 답한다).
"""

from __future__ import annotations

from datetime import datetime

import plotly.graph_objects as go

_VPIN_GOOD = "#009E73"
_VPIN_WARNING = "#E69F00"
_VPIN_CRITICAL = "#D55E00"
_VPIN_CRISIS_THRESHOLD = 0.7
_VPIN_WARNING_THRESHOLD = 0.4

# CVD 계열색. OFI의 #0072B2와 **다른 정체성**이어야 하고, VPIN 상태 팔레트(녹/주/홍)를 빌려
# 쓰면 안 된다(상태색은 예약색). 이 값은 눈대중이 아니라 팔레트 검증기를 돌려 고른 것 —
# #0072B2와 protan ΔE 8.5 / 정상시 ΔE 20.1, 흰 배경 대비 3:1 이상을 모두 통과한다.
_CVD_COLOR = "#A34E80"

# Absorption 막대색. OFI(#0072B2)·CVD(#A34E80)와 함께 검증기를 통과한 값이다(3색 동시 검사:
# 최악 인접쌍 protan ΔE 8.5 / 정상시 20.1, 대비 3:1 전부 통과). 문턱을 넘은 막대만 VPIN과
# **같은 심각색**을 쓴다 — 상태색은 예약색이고, 「임계 초과」라는 같은 뜻으로만 빌린다.
_ABSORPTION_COLOR = "#6E7B1F"
# v6 §10.1 «spike = current_vol / avg_vol, 3배 이상 + 가격 정체 = Absorption 의심».
_ABSORPTION_SUSPECT_THRESHOLD = 3.0
# 판정 불가(기준선 없음) 구간은 "봉 없음"과 **같은 회색**(`_GAP_BAND_COLOR`)으로 칠한다.
# 둘 다 값의 부재가 아니라 **판단의 부재**를 뜻하고, 화면에서 같은 뜻이면 같은 표시여야 한다.

# 파생 거래시간은 09:00~15:45로 고정(v6 §16.1) — 전일 장마감부터 익일 개장까지, 그리고 주말은
# 항상 거래가 없으므로 x축에서 건너뛴다. 그래야 실제 체결이 뜸한 옵션 종목도 시간축이 빈 공백에
# 눌리지 않고 거래시간끼리 이어 붙어 보인다(2026-07-07 사용자 지적).
_TRADING_HOURS_RANGEBREAKS = [
    dict(bounds=["sat", "mon"]),
    dict(bounds=[15.75, 9], pattern="hour"),
]

# 계열은 전부 1분봉이라 이웃한 두 봉의 간격은 정확히 60초다. 90초는 그 사이의 여유(스케줄 밀림)만
# 허용하는 값 — 이보다 벌어졌으면 그 사이에 **봉이 하나 이상 없다**는 뜻이므로 선을 잇지 않는다.
_GAP_BREAK_SECONDS = 90.0
# 음영까지 칠할 공백의 하한. `data_source._STALE_DATA_THRESHOLD_SECONDS`(결손 판정 5분)와 같은
# 기준을 쓴다 — 배지가 "결손"이라 부르는 것과 차트가 음영으로 표시하는 것이 어긋나면 안 된다.
# 1~2분짜리 공백까지 칠하면 거래가 얇은 옵션은 화면이 음영으로 뒤덮여 오히려 아무것도 안 보인다.
_GAP_BAND_MIN_SECONDS = 300.0
_GAP_BAND_COLOR = "#8A8A8A"


def _apply_trading_hours_rangebreaks(fig: go.Figure) -> None:
    fig.update_xaxes(rangebreaks=_TRADING_HOURS_RANGEBREAKS)


def _vpin_status_color(v: float) -> str:
    if v >= _VPIN_CRISIS_THRESHOLD:
        return _VPIN_CRITICAL
    if v >= _VPIN_WARNING_THRESHOLD:
        return _VPIN_WARNING
    return _VPIN_GOOD


def _break_on_gaps(
    timestamps: list[datetime], *series: list[float]
) -> tuple[list[datetime], list[list[float | None]]]:
    """
    입력: 봉 시각(오름차순)과 그에 정렬된 값 계열 1개 이상.
    계산: 봉 간격이 `_GAP_BREAK_SECONDS`를 넘는 자리마다 값이 `None`인 점을 하나 끼워 넣는다 —
         Plotly는 y가 None인 점에서 선을 끊으므로, 데이터가 없는 구간이 직선으로 이어지지 않는다.
    해석: 끼워 넣는 x는 공백의 **중간 지점**이다. 양 끝(마지막 봉/다음 봉)에 붙이면 그 봉의
         마커와 겹쳐 보이기 때문이다.
    실패 조건: 없음 — 점이 0~1개면 원본을 그대로 돌려준다(끊을 자리가 없다).
    """
    if len(timestamps) < 2:
        return list(timestamps), [list(s) for s in series]

    out_x: list[datetime] = [timestamps[0]]
    out_series: list[list[float | None]] = [[s[0]] for s in series]
    for i in range(1, len(timestamps)):
        if (timestamps[i] - timestamps[i - 1]).total_seconds() > _GAP_BREAK_SECONDS:
            out_x.append(timestamps[i - 1] + (timestamps[i] - timestamps[i - 1]) / 2)
            for out in out_series:
                out.append(None)
        out_x.append(timestamps[i])
        for out, src in zip(out_series, series):
            out.append(src[i])
    return out_x, out_series


def _gap_spans(timestamps: list[datetime]) -> list[tuple[datetime, datetime]]:
    """
    입력: 봉 시각(오름차순).
    계산: 음영으로 표시할 공백 구간 목록. `_GAP_BAND_MIN_SECONDS`를 넘고 **같은 날 안에서**
         생긴 공백만 고른다.
    해석: 날짜를 넘는 공백(야간·주말)은 결손이 아니라 정상이고, `_TRADING_HOURS_RANGEBREAKS`가
         이미 x축에서 접어버린다 — 그것까지 "봉 없음"으로 칠하면 상시 오경보가 된다(CB 하트비트
         에서 배운 것과 같은 실수).
    실패 조건: 없음 — 해당 없으면 빈 목록.
    """
    return [
        (timestamps[i - 1], timestamps[i])
        for i in range(1, len(timestamps))
        if (timestamps[i] - timestamps[i - 1]).total_seconds() >= _GAP_BAND_MIN_SECONDS
        and timestamps[i - 1].date() == timestamps[i].date()
    ]


def _add_gap_bands(fig: go.Figure, timestamps: list[datetime]) -> None:
    """공백 구간을 회색 음영 + "봉 없음" 라벨로 표시한다 — 값이 아니라 **관측의 부재**를 그린다."""
    for start, end in _gap_spans(timestamps):
        fig.add_vrect(
            x0=start,
            x1=end,
            fillcolor=_GAP_BAND_COLOR,
            opacity=0.18,
            line_width=0,
            layer="below",
            annotation_text=f"봉 없음 {(end - start).total_seconds() / 60:.0f}분",
            annotation_position="top left",
            annotation_font_size=10,
        )


def shared_x_range(*timestamp_series: list[datetime]) -> tuple[datetime, datetime] | None:
    """
    입력: 같은 x축을 쓰게 할 봉 시각 계열들(선물 계열, 옵션 계열 …).
    계산: 모든 계열 **합집합**의 최소~최대 시각. 점이 사실상 하나뿐이면(폭 0) None.
    해석: Flow Radar 두 벌(옵션·선물)의 여덟 차트가 전부 같은 가로 좌표를 쓰게 하는 값이다.
         2026-08-21 사용자 지적 — 종전에는 옵션 차트에만 선물 창을 `x_range`로 강제하고 선물
         차트는 아무것도 안 넘겼다. Plotly의 자동 여백은 계열의 값 분포에 따라 달라지므로 같은
         09:30이 두 그림에서 서로 다른 가로 위치에 찍혔고, 위아래를 눈으로 대조할 수 없었다.
         선물 범위가 아니라 **합집합**인 이유: 선물 창만 강제하면 그 밖에 있는 옵션 봉이
         보이지도 않은 채 잘려 나간다(2026-08-05 P2-9에서 창 밖 점이 y축만 잡아늘였던 것과
         같은 종류의 "안 보이는데 영향은 주는" 상태).
    실패 조건: 없음 — 범위를 못 정하면 None을 돌려 Plotly 자동 범위에 맡긴다. 그때는 애초에
              그릴 점이 0~1개뿐이라 맞출 축 자체가 없다.
    """
    all_timestamps = [ts for series in timestamp_series for ts in series]
    if not all_timestamps:
        return None
    low, high = min(all_timestamps), max(all_timestamps)
    return (low, high) if low < high else None


def build_cvd_chart(
    timestamps: list[datetime], cvd_series: list[float], x_range: tuple[datetime, datetime] | None = None
) -> go.Figure:
    """CVD(누적 체결 델타) — 창 시작 이후 `매수체결량 − 매도체결량`의 누적합.

    OFI와 **같은 그림에 겹치지 않는다.** OFI는 봉마다 0 근처를 오가는 진동값이고 CVD는 단조에
    가깝게 누적되는 값이라 스케일이 두 자릿수 이상 벌어진다 — 이중 y축으로 겹치면 두 선의
    교차점이 축 스케일이 만든 우연이 되어 아무 뜻도 없는 "신호"처럼 읽힌다. 두 계열은 x축만
    공유하는 별도 차트로 위아래에 둔다.

    값의 원점은 **화면 창의 시작**이다(종목 전체 누적이 아니다) — 창이 밀리면 같은 시각의 CVD
    절대값도 달라지므로, 읽을 것은 절대 높이가 아니라 **기울기와 부호 전환**이다.

    ⚠ **2026-08-21 이전에 수집된 봉은 매수 쪽으로 편향돼 있다.** 이 차트가 만든 결함이 아니라
    **드러낸** 결함이다 — 종전 `collector.MinuteBarAggregator`의 분류가 `p >= prev_price`라
    동가 틱이 전부 매수로 갔고, baseline이 그 봉 자신의 첫 체결가라 첫 틱도 항상 매수였다.
    가격이 제자리인 한 시간 동안 이 차트가 부호 전환 없이 85 -> 2,454까지 곧게 올라 발견됐다
    (08-21 09:38~10:38 A01609 실측, 그 창의 매수비율 59.7%).

    **분류는 고쳤다**(`_classify_volumes` — 동가 틱은 직전 분류 승계, baseline은 봉 경계를 넘어
    이월). 그러나 **틱을 저장하지 않으므로 과거 봉은 재계산할 수 없다.** 즉 이 차트는

        수정 배포 이후 수집된 봉  ->  신뢰 가능
        그 이전에 수집된 봉        ->  매수 편향 잔존(영구)

    이다. 창(`FLOW_RADAR_WINDOW_MINUTES`)이 그 경계를 가로지르는 동안에는 계단이 하나 생긴다 —
    시장이 만든 것이 아니다.
    """
    x, (y,) = _break_on_gaps(timestamps, cvd_series)
    fig = go.Figure(
        go.Scatter(
            x=x,
            y=y,
            mode="lines+markers",
            line=dict(color=_CVD_COLOR, width=2),
            marker=dict(color=_CVD_COLOR, size=5),
            hovertemplate="%{x|%H:%M}: CVD %{y:+,.0f}<extra></extra>",
        )
    )
    fig.add_hline(y=0, line_color="#8A8A8A", line_width=1)
    _add_gap_bands(fig, timestamps)
    fig.update_layout(
        yaxis_title="CVD(창 시작=0)", showlegend=False, margin=dict(l=10, r=10, t=10, b=10), height=180
    )
    _apply_trading_hours_rangebreaks(fig)
    if x_range is not None:
        fig.update_xaxes(range=list(x_range))
    return fig


def _no_baseline_spans(
    timestamps: list[datetime], absorption_series: list[float | None]
) -> list[tuple[datetime, datetime]]:
    """
    입력: 봉 시각과 그에 정렬된 Absorption 계열(`None` = 기준선 없음).
    계산: `None`이 이어지는 구간의 (시작, 끝) 목록.
    해석: 창 앞머리에서 «아직 기준선을 못 만들었다»가 대부분이지만, 중간에 생길 수도 있다
         (예: `open`이 비정상인 봉). 어디든 **판정하지 않은 구간**이므로 똑같이 칠한다.
    실패 조건: 없음 — 해당 없으면 빈 목록.
    """
    spans: list[tuple[datetime, datetime]] = []
    start: datetime | None = None
    for ts, value in zip(timestamps, absorption_series):
        if value is None:
            if start is None:
                start = ts
            end = ts
        elif start is not None:
            spans.append((start, end))
            start = None
    if start is not None:
        spans.append((start, end))
    return spans


def build_absorption_chart(
    timestamps: list[datetime],
    absorption_series: list[float | None],
    x_range: tuple[datetime, datetime] | None = None,
    price_change_threshold: float = 0.0005,
) -> go.Figure:
    """Absorption(흡수) 배수 — 「대량 체결에도 가격이 안 움직였는가」(v6 §8.2·§10.1).

    **선이 아니라 막대다.** 이 값은 이어지는 수준이 아니라 봉마다 독립인 사건 크기이고,
    대부분의 봉에서 0이다(가격이 문턱을 넘게 움직이면 `absorption_score()`가 0을 돌린다).
    선으로 그리면 0이 이어진 구간이 「관측 중이고 값이 0」이 아니라 평탄한 신호처럼 보인다.

    **0과 「모름」을 다르게 그린다.** 높이 0인 막대는 안 보이므로 그것만으로는 «판정했고 흡수가
    아니다»와 «판정 못 했다»가 구분되지 않는다 — 이 저장소가 08-05(미관측을 정상으로 표시)와
    08-21(모르는 것을 매수로 분류)에서 두 번 겪은 실수다. 그래서

        판정했고 0      ->  y=0 자리에 작은 점 (관측했다는 표시)
        기준선 없음     ->  점도 막대도 없고, 회색 음영 + "기준선 없음" 라벨

    **문턱선은 값이 아무리 작아도 항상 화면에 있다.** y축 상한을 최소 `3.5`로 고정하기 때문이다.
    자동 범위에 맡기면 최대값이 1.4인 날에 3배 선이 화면 밖으로 나가고, 그러면 «임계에 얼마나
    가까운가»를 읽을 수 없다.

    **「가격 정체」의 문턱은 종목마다 다른 값이다.** 봉 범위가 직전 20봉 범위 중앙값의 절반
    이하(최소 2틱)일 때만 흡수 후보로 본다 — 고정 상수는 두 상품 중 한쪽에서 반드시 무의미해진다
    (`features.orderflow.flat_range_limit` docstring의 실측 표). 화면 캡션이 이 기준을 적는다.
    """
    observed_x = [ts for ts, v in zip(timestamps, absorption_series) if v is not None]
    observed_y = [v for v in absorption_series if v is not None]
    colors = [
        _VPIN_CRITICAL if v >= _ABSORPTION_SUSPECT_THRESHOLD else _ABSORPTION_COLOR for v in observed_y
    ]

    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=observed_x,
            y=observed_y,
            name="흡수 배수",
            marker=dict(color=colors),
            hovertemplate="%{x|%H:%M}: 거래량 평균 대비 %{y:.2f}배<extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=observed_x,
            y=[0] * len(observed_x),
            mode="markers",
            name="판정함(흡수 아님)",
            marker=dict(color="#8A8A8A", size=3),
            hoverinfo="skip",
        )
    )
    fig.add_hline(
        y=_ABSORPTION_SUSPECT_THRESHOLD,
        line_dash="dash",
        line_color=_VPIN_CRITICAL,
        annotation_text=f"흡수 의심({_ABSORPTION_SUSPECT_THRESHOLD:.0f}배)",
        # 기본값(top right)은 화면 오른쪽 끝에 결손 음영이 걸린 날 "봉 없음 N분" 라벨과 겹친다
        # (08-21 렌더에서 실제로 뭉개졌다). 선 아래로 내려 같은 높이를 다투지 않게 한다.
        annotation_position="bottom right",
    )
    for start, end in _no_baseline_spans(timestamps, absorption_series):
        fig.add_vrect(
            x0=start,
            x1=end,
            fillcolor=_GAP_BAND_COLOR,
            opacity=0.18,
            line_width=0,
            layer="below",
            annotation_text="기준선 없음",
            # "봉 없음"은 top left에 붙는다. 두 음영이 겹치는 자리(창 앞머리에 결손이 있는
            # 옵션 계열에서 실제로 생긴다 — 08-21 렌더에서 두 라벨이 한 줄로 뭉개졌다)에서
            # 서로 덮지 않도록 이쪽만 아래로 내린다.
            annotation_position="bottom left",
            annotation_font_size=10,
        )
    _add_gap_bands(fig, timestamps)
    top = max(_ABSORPTION_SUSPECT_THRESHOLD + 0.5, max(observed_y, default=0.0) * 1.15)
    fig.update_layout(
        yaxis=dict(title="Absorption(배)", range=[0, top]),
        legend=dict(orientation="h", y=1.15),
        margin=dict(l=10, r=10, t=30, b=10),
        height=200,
        bargap=0.15,
    )
    _apply_trading_hours_rangebreaks(fig)
    if x_range is not None:
        fig.update_xaxes(range=list(x_range))
    return fig


def build_ofi_sparkline(
    timestamps: list[datetime], ofi_series: list[float], x_range: tuple[datetime, datetime] | None = None
) -> go.Figure:
    # mode="lines"만 쓰면 점이 1개뿐인 계열(거래가 뜸한 옵션 등)은 Plotly가 선을 그릴 수 없어
    # 아무것도 안 보인다(2026-07-06 실데이터로 발견) — 마커를 항상 같이 그려 최소 1개 점은 보이게 한다.
    x, (y,) = _break_on_gaps(timestamps, ofi_series)
    fig = go.Figure(
        go.Scatter(
            x=x,
            y=y,
            mode="lines+markers",
            line=dict(color="#0072B2", width=2),
            marker=dict(color="#0072B2", size=5),
            hovertemplate="%{x|%H:%M}: OFI %{y:.0f}<extra></extra>",
        )
    )
    fig.add_hline(y=0, line_color="#8A8A8A", line_width=1)
    _add_gap_bands(fig, timestamps)
    fig.update_layout(yaxis_title="OFI", showlegend=False, margin=dict(l=10, r=10, t=10, b=10), height=180)
    _apply_trading_hours_rangebreaks(fig)
    if x_range is not None:
        fig.update_xaxes(range=list(x_range))
    return fig


def build_vpin_chart(
    timestamps: list[datetime], vpin_series: list[float], x_range: tuple[datetime, datetime] | None = None
) -> go.Figure:
    x, (y,) = _break_on_gaps(timestamps, vpin_series)
    # 색은 끊김 점(None)이 끼워진 **뒤의** 계열 기준으로 매긴다 — 원본 기준으로 매기면 색과 값이
    # 한 칸씩 어긋난다. 끊김 점 자체는 그릴 값이 없으므로 양호색을 두되 마커가 나오지 않는다.
    colors = [_VPIN_GOOD if v is None else _vpin_status_color(v) for v in y]
    fig = go.Figure(
        go.Scatter(
            x=x,
            y=y,
            mode="lines+markers",
            line=dict(color="#8A8A8A", width=1),
            marker=dict(color=colors, size=6),
            hovertemplate="%{x|%H:%M}: VPIN %{y:.2f}<extra></extra>",
        )
    )
    fig.add_hline(y=_VPIN_CRISIS_THRESHOLD, line_dash="dash", line_color=_VPIN_CRITICAL, annotation_text="독성 임계(0.7)")
    _add_gap_bands(fig, timestamps)
    fig.update_layout(
        yaxis=dict(title="VPIN", range=[0, 1]), showlegend=False, margin=dict(l=10, r=10, t=10, b=10), height=200
    )
    _apply_trading_hours_rangebreaks(fig)
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
    x, (price, micro) = _break_on_gaps(timestamps, price_series, microprice_series)
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=x, y=price, mode="lines+markers", name="체결가",
            line=dict(color="#8A8A8A", width=2), marker=dict(color="#8A8A8A", size=5),
        )
    )
    fig.add_trace(
        go.Scatter(
            x=x, y=micro, mode="lines+markers", name="Microprice",
            line=dict(color="#0072B2", width=2, dash="dot"), marker=dict(color="#0072B2", size=5),
        )
    )
    _add_gap_bands(fig, timestamps)
    fig.update_layout(
        yaxis_title="가격", legend=dict(orientation="h", y=1.15), margin=dict(l=10, r=10, t=30, b=10), height=220
    )
    _apply_trading_hours_rangebreaks(fig)
    if x_range is not None:
        fig.update_xaxes(range=list(x_range))
    return fig
