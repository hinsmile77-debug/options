"""Cross-asset stress 패널 (v6 §7.3·§5.1, 2026-07-10 신규; 2026-07-20 ES/MOVE/USDKRW 추가) —
VIX 기간구조·USDCNH·US10Y·USDKRW·S&P500 선물·MOVE를 장전/5분 주기 매크로 리스크 필터로 한눈에
보여준다.

VIX 선물(CBOE VX)·USDCNH 선물(HKEx CNH)은 5분마다 갱신되지만, us10y_yield/usdkrw(실제 레벨)는
해외주식 종목_지수_환율기간별시세 일봉 API로만 얻어(mahdi/main.py poll_macro_snapshot 주석 참고)
하루 중 값이 드물게만 바뀐다 — ZN/ES 선물가가 있으면 그쪽을 5분 주기 "급변" 판단의 1차 신호로
쓰고, US10Y/USDKRW는 레벨 참고용 보조열로 함께 보여준다. ZN(CME/CBOT 10년 국채선물)·ES(CME
E-mini S&P500 선물)는 KIS 유료 항목(2026-07-20 HTS [7936] 확인: 월 228.8불)이라 모의투자 개발
단계에서는 미구독 — *_source가 "yfinance_fallback"이면 실제 체결가가 아니라 mahdi/data/
yfinance_fallback.py의 비공식 근사치임을 표에 함께 표시한다. MOVE(ICE BofA MOVE Index)는 장외
파생 인덱스라 KIS 경로 자체가 없어 항상 폴백에서만 온다.
"""

from __future__ import annotations

import plotly.graph_objects as go


def _term_structure_label(value: float | None) -> str:
    if value is None:
        return "-"
    direction = "콘탱고" if value >= 0 else "백워데이션"
    return f"{value * 100:+.2f}% ({direction})"


def _fallback_labeled(value: float | None, source: str | None, fmt: str) -> str:
    """값이 있고 출처가 yfinance_fallback이면 "(폴백)"을 붙여 실제 체결가와 구분한다."""
    if value is None:
        return "-"
    text = format(value, fmt)
    return f"{text} (폴백)" if source == "yfinance_fallback" else text


def _aged(text: str, asof, snapshot_ts) -> str:
    """
    입력: 이미 서식이 적용된 값 문자열, 그 값의 관측 시각(`*_asof`), 스냅샷 행의 시각.
    계산: 두 시각의 **날짜가 다르면** 값 뒤에 관측 날짜(MM-DD)를 붙인다.
    해석: 2026-08-05(P1-4). LOCF(forward-fill)로 실려온 값은 최신 행의 시각과 무관하게 며칠 전
         것일 수 있는데, 종전 표에는 시각이 **하나도 없어** 그것을 알 방법이 없었다. 같은 날
         안에서의 이월(일봉 항목이 6시간 주기로 갱신되는 등)은 정상이라 표기하지 않는다 —
         전부 표기하면 매 행에 날짜가 붙어 정작 며칠 전 값이 눈에 안 띈다.
    실패 조건: 시각을 모르거나 값이 없으면("-") 그대로 돌려준다.
    """
    if text == "-" or asof is None or snapshot_ts is None or asof.date() == snapshot_ts.date():
        return text
    return f"{text} ({asof:%m-%d})"


def build_macro_snapshot_table(snapshot: dict | None) -> go.Figure:
    """
    입력: mahdi.data.db.latest_macro_snapshot()의 반환값(dict) 또는 None(폴링이 아직 안 돌았음).
    계산: VIX 근월·차근월·기간구조(콘탱고 양수/백워데이션 음수), USDCNH, ZN/ES 선물(근월물, 5분
         급변 감지용), US10Y/USDKRW(일봉 레벨, 참고용), MOVE(장외 인덱스)를 Plotly 표 1행으로
         렌더링한다. 기간구조는 부호로 콘탱고/백워데이션을 함께 표시해 숫자만으로는 바로 안 읽히는
         리스크 신호(백워데이션=단기 스트레스 급등)를 한눈에 보이게 한다.
    해석: 값이 없는 필드는 "-"로 표시한다 — US10Y/USDKRW는 하루 중 대부분 NULL일 수 있고,
         ZN/ES/MOVE도 KIS·yfinance 폴백이 둘 다 실패하면 NULL이므로 에러가 아니다. ZN/ES/MOVE
         값이 yfinance 폴백에서 온 경우(각 *_source == "yfinance_fallback")는 "(폴백)"을 붙여
         실제 체결가와 구분한다.

         2026-08-05(P1-4): **첫 열에 스냅샷 기준 시각을 쓴다.** 종전에는 표에 시각이 하나도 없어
         폴러가 죽어도 며칠 전 값이 "지금"으로 보였다(그리고 LOCF에 소급 상한도 없었다).
         LOCF로 실려온 값은 날짜가 다를 때만 뒤에 관측 날짜를 붙인다(`_aged`).
    """
    row = snapshot or {}
    snapshot_ts = row.get("timestamp")
    as_of = f"{snapshot_ts:%m-%d %H:%M}" if snapshot_ts is not None else "-"
    vix_front = f"{row['vix_front']:.2f}" if row.get("vix_front") is not None else "-"
    vix_next = f"{row['vix_next']:.2f}" if row.get("vix_next") is not None else "-"
    term_structure = _term_structure_label(row.get("vix_term_structure"))
    usdcnh = f"{row['usdcnh']:.4f}" if row.get("usdcnh") is not None else "-"
    zn_front = _aged(
        _fallback_labeled(row.get("zn_front"), row.get("zn_front_source"), ".4f"),
        row.get("zn_front_asof"), snapshot_ts,
    )
    es_front = _fallback_labeled(row.get("es_front"), row.get("es_front_source"), ".4f")
    move_index = _aged(
        _fallback_labeled(row.get("move_index"), row.get("move_index_source"), ".2f"),
        row.get("move_index_asof"), snapshot_ts,
    )
    us10y = _aged(
        f"{row['us10y_yield']:.2f}%" if row.get("us10y_yield") is not None else "-",
        row.get("us10y_yield_asof"), snapshot_ts,
    )
    usdkrw = _aged(
        f"{row['usdkrw']:.2f}" if row.get("usdkrw") is not None else "-",
        row.get("usdkrw_asof"), snapshot_ts,
    )

    fig = go.Figure(
        go.Table(
            header=dict(
                values=[
                    "기준 시각", "VIX 근월", "VIX 차근월", "VIX 기간구조", "USDCNH", "ZN(10Y 선물) 근월",
                    "US10Y(일봉 레벨)", "USDKRW(일봉 레벨)", "ES(S&P500 선물) 근월", "MOVE Index",
                ],
                align="center",
            ),
            cells=dict(
                values=[
                    [as_of], [vix_front], [vix_next], [term_structure], [usdcnh], [zn_front],
                    [us10y], [usdkrw], [es_front], [move_index],
                ],
                align="center",
            ),
        )
    )
    fig.update_layout(margin=dict(l=10, r=10, t=10, b=10), height=120)
    return fig
