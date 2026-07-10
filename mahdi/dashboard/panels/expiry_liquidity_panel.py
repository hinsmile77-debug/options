"""만기 유동성 비교 패널 (Phase 1.5-④, v6 §17 COCKPIT 확장).

먼슬리("regular")/위클리("weekly") 두 북의 ATM±2 유동성 스냅샷을 나란히 비교한다 —
장전 선발 스코어러(Phase 2, 아직 미구현)가 20거래일 기준선으로 쓸 원자료를 사람이 먼저
눈으로 확인할 수 있게 한다. 근거: docs/Dev_md/RESEARCH_EXPIRY_SELECTION_v1.md.
"""

from __future__ import annotations

from datetime import date

import plotly.graph_objects as go

_SERIES_LABEL_KO = {"regular": "먼슬리", "weekly": "위클리"}

_MONTHLY_EXPIRY_WEEK_NOTE = (
    "※ 이번 주는 먼슬리 만기 주 — 목요일 위클리 신규 상장이 없어 먼슬리가 그 역할을 대신합니다"
    "(위클리 행은 차주 위클리를 관측 중)."
)


def _is_monthly_expiry_week(rows: list[dict], today: date) -> bool:
    """
    계산: "regular" 북의 만기가 today와 같은 (연도, ISO주차)에 속하면 이번 주가 먼슬리
         만기 주라고 판단한다. KRX는 먼슬리 만기 주의 목요일에는 위클리를 별도 상장하지
         않으므로(2026-07-10 확인), 이 경우 "weekly" 행은 자동으로 차주 위클리를 가리키게
         된다 — 사용자가 데이터 누락(대시)으로 오인하지 않도록 명시적으로 알려준다.
    """
    for r in rows:
        if r.get("series") != "regular":
            continue
        expiry = r.get("expiry")
        if expiry is not None and expiry.isocalendar()[:2] == today.isocalendar()[:2]:
            return True
    return False


def build_expiry_liquidity_table(rows: list[dict], today: date | None = None) -> go.Figure:
    """
    입력: [{"series", "expiry", "atm_spread_pct", "depth", "volume", "days_to_expiry"}, ...]
         (data_source.DashboardSnapshot.expiry_liquidity — 북당 최신 1행씩). today는 "이번 주가
         먼슬리 만기 주인가" 판정 기준(기본값 오늘) — 테스트에서 고정 날짜 주입용으로도 쓴다.
    계산: Plotly 표로 렌더링. %스프레드는 Cao & Wei(2010) 권고에 따른 상대(%) 스프레드이지
         달러 스프레드가 아니므로 "%" 단위로 표시한다.
    """
    today = today if today is not None else date.today()
    labels = [_SERIES_LABEL_KO.get(r["series"], r["series"]) for r in rows]
    expiries = [r["expiry"].isoformat() if r.get("expiry") else "-" for r in rows]
    spreads = [f"{r['atm_spread_pct'] * 100:.2f}%" if r.get("atm_spread_pct") is not None else "-" for r in rows]
    depths = [f"{r['depth']:,.0f}" if r.get("depth") is not None else "-" for r in rows]
    volumes = [f"{r['volume']:,.0f}" if r.get("volume") is not None else "-" for r in rows]
    days = [str(r["days_to_expiry"]) if r.get("days_to_expiry") is not None else "-" for r in rows]

    fig = go.Figure(
        go.Table(
            header=dict(
                values=["북", "만기", "ATM±2 %스프레드", "호가잔량 합(깊이)", "누적거래량", "잔존일수"],
                align="center",
            ),
            cells=dict(values=[labels, expiries, spreads, depths, volumes, days], align="center"),
        )
    )
    if _is_monthly_expiry_week(rows, today):
        fig.update_layout(
            margin=dict(l=10, r=10, t=36, b=10),
            height=196,
            title=dict(text=_MONTHLY_EXPIRY_WEEK_NOTE, font=dict(size=12), x=0, xanchor="left"),
        )
    else:
        fig.update_layout(margin=dict(l=10, r=10, t=10, b=10), height=160)
    return fig
