"""Flow Radar 패널 — OFI 스파크라인, VPIN 독성 게이지, Microprice vs 체결가 (v6 §8, §17 COCKPIT).

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
