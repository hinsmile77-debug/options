"""Cross-asset stress 패널 (v6 §7.3·§5.1, 2026-07-10 신규) — VIX 기간구조·USDCNH·US10Y를
장전/5분 주기 매크로 리스크 필터로 한눈에 보여준다.

VIX 선물(CBOE VX)·USDCNH 선물(HKEx CNH)·ZN 선물(CME/CBOT 10년 국채선물, 2026-07-10 사용자가
계좌에 CBOT 거래소 신청을 완료한 뒤 추가)은 5분마다 갱신되지만, us10y_yield(실제 수익률 %)는
해외주식 국채 일봉 API로만 얻어(mahdi/main.py poll_macro_snapshot 주석 참고) 하루 중 값이
드물게만 바뀐다 — ZN 선물가가 있으면 그쪽을 5분 주기 "급변" 판단의 1차 신호로 쓰고, US10Y는
레벨 참고용 보조열로 함께 보여준다.
"""

from __future__ import annotations

import plotly.graph_objects as go


def _term_structure_label(value: float | None) -> str:
    if value is None:
        return "-"
    direction = "콘탱고" if value >= 0 else "백워데이션"
    return f"{value * 100:+.2f}% ({direction})"


def build_macro_snapshot_table(snapshot: dict | None) -> go.Figure:
    """
    입력: mahdi.data.db.latest_macro_snapshot()의 반환값(dict) 또는 None(폴링이 아직 안 돌았음).
    계산: VIX 근월·차근월·기간구조(콘탱고 양수/백워데이션 음수), USDCNH, ZN 선물(근월물, 5분
         급변 감지용), US10Y(일봉 레벨, 참고용)를 Plotly 표 1행으로 렌더링한다. 기간구조는
         부호로 콘탱고/백워데이션을 함께 표시해 숫자만으로는 바로 안 읽히는 리스크 신호
         (백워데이션=단기 스트레스 급등)를 한눈에 보이게 한다.
    해석: 값이 없는 필드는 "-"로 표시한다 — US10Y는 CBOT 미신청 계좌에서 하루 중 대부분
         NULL일 수 있고, ZN 자체도 CBOT 거래소 신청 전에는 항상 NULL이므로 둘 다 에러가 아니다.
    """
    row = snapshot or {}
    vix_front = f"{row['vix_front']:.2f}" if row.get("vix_front") is not None else "-"
    vix_next = f"{row['vix_next']:.2f}" if row.get("vix_next") is not None else "-"
    term_structure = _term_structure_label(row.get("vix_term_structure"))
    usdcnh = f"{row['usdcnh']:.4f}" if row.get("usdcnh") is not None else "-"
    zn_front = f"{row['zn_front']:.4f}" if row.get("zn_front") is not None else "-"
    us10y = f"{row['us10y_yield']:.2f}%" if row.get("us10y_yield") is not None else "-"

    fig = go.Figure(
        go.Table(
            header=dict(
                values=["VIX 근월", "VIX 차근월", "VIX 기간구조", "USDCNH", "ZN(10Y 선물) 근월", "US10Y(일봉 레벨)"],
                align="center",
            ),
            cells=dict(
                values=[[vix_front], [vix_next], [term_structure], [usdcnh], [zn_front], [us10y]], align="center"
            ),
        )
    )
    fig.update_layout(margin=dict(l=10, r=10, t=10, b=10), height=120)
    return fig
