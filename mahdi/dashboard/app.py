"""COCKPIT v1 — 관측 전용 Streamlit 대시보드 (v6 §17, PART 21 Phase1 체크리스트).

Regime · Gamma Map · Flow Radar · 수급 패널만 표시한다. 주문 실행/승인 UI(Action Feed,
원클릭 승인 등)는 COCKPIT v2(Phase3) 범위다.
"""

from __future__ import annotations

import time

import streamlit as st

from mahdi.dashboard.data_source import (
    get_account_status_view,
    get_health_summary,
    get_latest_decision_context,
    get_market_halt_status,
    get_slack_alerts_enabled,
    load_snapshot,
    record_cockpit_startup,
    set_slack_alerts_enabled,
)
from mahdi.dashboard.panels.account_panel import build_account_summary_cards
from mahdi.dashboard.panels.decision_panel import build_decision_history_table, build_decision_summary_cards
from mahdi.dashboard.panels.flow_radar_panel import (
    build_microprice_vs_price_chart,
    build_ofi_sparkline,
    build_vpin_chart,
)
from mahdi.dashboard.panels.expiry_liquidity_panel import build_expiry_liquidity_table
from mahdi.dashboard.panels.gamma_map_panel import build_gamma_profile_chart
from mahdi.dashboard.panels.macro_panel import build_macro_snapshot_table
from mahdi.dashboard.panels.position_panel import build_position_flow_chart
from mahdi.dashboard.panels.regime_panel import REGIME_LABEL_KO, build_regime_probability_chart

_CARD_BADGE = {"ok": st.success, "warning": st.warning, "info": st.info, "neutral": st.info}


def _render_cards(cards: list[dict]) -> None:
    """카드 dict(label/value/status/help) 리스트를 st.columns 배지로 렌더링한다 — "3초 룰"
    (스크롤 없이 한눈에 파악)을 위해 `get_health_summary()`의 기존 배지 스타일을 그대로 재사용.
    향후 카드가 늘어나도 이 함수는 그대로고, 호출측 리스트에 dict만 추가하면 된다."""
    cols = st.columns(len(cards))
    for col, card in zip(cols, cards):
        badge = _CARD_BADGE.get(card["status"], st.info)
        with col:
            badge(f"**{card['label']}**\n\n{card['value']}")
            if card.get("help"):
                st.caption(card["help"])

st.set_page_config(page_title="마흐디 COCKPIT v1", layout="wide")

REFRESH_INTERVAL_SECONDS = 10  # 1분봉 적재 주기보다 짧게 잡아 새 봉을 빠르게 반영


@st.cache_resource
def _log_cockpit_startup_once() -> None:
    # 2026-07-22(운영점검보고서 §1-1) — 이 스크립트는 REFRESH_INTERVAL_SECONDS마다 st.rerun()으로
    # 처음부터 다시 실행되지만, st.cache_resource로 감싸면 실제 프로세스당 딱 1회만 호출된다.
    # 좀비 프로세스(전날 떠서 안 죽고 남아있는 것)를 로그만으로 즉시 구분할 수 있게 한다.
    print(record_cockpit_startup(), flush=True)


_log_cockpit_startup_once()


def render() -> None:
    snapshot = load_snapshot()

    st.title("마흐디 COCKPIT — 관측 전용 (Phase 1)")

    # 2026-07-29 신규 — 거래소 서킷브레이커/거래정지(mahdi.risk.market_halt) 실시간 감지. 평시엔
    # 아무것도 그리지 않는다(상시 배지는 "오늘의 점검 요약" 그리드가 이미 맡고 있음) — 발동 중일
    # 때만 스크롤 없이 즉시 눈에 띄어야 하므로 st.error로 최상단에 크게 띄운다.
    halt_status = get_market_halt_status()
    if halt_status and halt_status["is_halted"]:
        st.error(
            f"🚨 거래소 서킷브레이커/거래정지 발동 중 — {halt_status['label']}"
            f"(코드 {halt_status['mkop_cls_code']}, {halt_status['halted_since']:%H:%M:%S}부터) — "
            f"신규 진입 자동 차단됨"
        )

    if not snapshot.is_live:
        st.warning("DB에서 데이터를 찾지 못해 합성 리플레이 데이터로 표시 중입니다 (독립 실행 모드).")

    # 2026-07-19(§5-4 "능동 알림 도입") — Slack On/Off. COCKPIT과 관측 루프(mahdi.main)는 서로
    # 다른 프로세스라 DB(slack_alert_settings)를 통해 값을 주고받는다 — 여기서 저장하면 재시작
    # 없이 mahdi.main의 다음 알림 시도부터 바로 반영된다.
    slack_col, _ = st.columns([1, 5])
    with slack_col:
        current_slack_enabled = get_slack_alerts_enabled()
        slack_toggle = st.checkbox(
            "🔔 슬랙 알림",
            value=current_slack_enabled,
            key="slack_alert_toggle",
            help="option_analysis_1m 결손·CBOT 계좌 미승인·WS 연결 끊김 등 이상 상황을 Slack으로 알립니다.",
        )
        if slack_toggle != current_slack_enabled:
            set_slack_alerts_enabled(slack_toggle)

    # 2026-07-19(§5-6 "오늘의 점검 요약") — 운영점검보고서 §1-B 장중 체크리스트 중 SQL로
    # 자동화 가능한 항목들(데이터 결손율·CBOT 상태·series 화이트리스트 위반·레짐 stability_flag
    # 비율)을 매번 사람이 DB를 직접 조회하지 않고 상단 배지로 상시 노출한다.
    # 2026-08-01(§5-5): 배지가 11 → 15개가 되면서 한 줄에 다 펴면 각 열이 너무 좁다. "인프라"와
    # "관측 품질"을 나눠 2행으로 낸다 — 07-31에 **인프라 지표는 전부 좋아졌는데 판단 입력 품질
    # (먼슬리 커버리지)은 오히려 후퇴한** 사례가 있었고, 두 그룹을 나란히 놓아야 그게 보인다.
    st.subheader("오늘의 점검 요약")
    health_checks = get_health_summary()
    groups: dict[str, list] = {}
    for check in health_checks:
        groups.setdefault(check.group, []).append(check)
    for group_name, checks in groups.items():
        st.caption(group_name)
        for col, check in zip(st.columns(len(checks)), checks):
            badge = {"ok": col.success, "warning": col.warning}.get(check.status, col.info)
            badge(f"**{check.label}**\n\n{check.detail}")

    # 2026-07-29 신규 — ADVISORY 모드지만 마흐디가 지금 어떤 진입 판단을 내리고 있는지(청산
    # 단계는 ExecutionEngine 미배선이라 자리만 확보) + 계좌 현황/수익률을 "3초 룰"로 최상단에
    # 노출한다(운영점검보고서 2026-07-29 요청 사항).
    st.subheader("마흐디 판단 현황 (ADVISORY — 참고용, 실주문 없음)")
    decision_context = get_latest_decision_context()
    _render_cards(build_decision_summary_cards(decision_context["latest"]))
    if decision_context["history"]:
        st.plotly_chart(build_decision_history_table(decision_context["history"]), width='stretch')
    else:
        st.caption("아직 Signal Fusion 판단 이력이 없습니다.")

    st.subheader("계좌 현황")
    _render_cards(build_account_summary_cards(get_account_status_view()))

    col1, col2, col3 = st.columns(3)
    col1.metric("현재 레짐", REGIME_LABEL_KO[snapshot.regime])
    col2.metric("기초자산 현재가", f"{snapshot.spot:,.2f}")
    col3.metric("레짐 안정성", "안정" if snapshot.stability_flag else "REGIME_UNSTABLE")

    st.subheader("Regime")
    st.plotly_chart(build_regime_probability_chart(snapshot.regime_prob), width='stretch')

    col_left, col_right = st.columns(2)
    with col_left:
        st.subheader("Gamma Map")
        strikes = [c.strike for c in snapshot.chain]
        gex = [c.gex for c in snapshot.chain]
        st.plotly_chart(
            build_gamma_profile_chart(strikes, gex, snapshot.spot, snapshot.gamma_flip, snapshot.gamma_walls),
            width='stretch',
        )
    with col_right:
        st.subheader("수급 (Position Intelligence)")
        st.plotly_chart(
            build_position_flow_chart(snapshot.foreign_net, snapshot.institution_net, snapshot.individual_net),
            width='stretch',
        )

    st.subheader("Cross-asset Stress (VIX 기간구조·USDCNH·US10Y)")
    st.plotly_chart(build_macro_snapshot_table(snapshot.macro_snapshot), width='stretch')
    if snapshot.macro_snapshot is None:
        st.caption("아직 매크로 스냅샷 폴링 데이터가 없습니다.")
    elif snapshot.macro_snapshot.get("us10y_yield") is None:
        st.caption("US10Y는 계좌에 CBOT 거래소 신청이 안 되어 있는 동안 일봉으로만 갱신됩니다 — 값이 채워지기 전까지는 정상적으로 비어 있습니다.")

    st.subheader("만기 유동성 비교 (먼슬리 vs 위클리(월) vs 위클리(목))")
    if snapshot.expiry_liquidity:
        st.plotly_chart(
            build_expiry_liquidity_table(snapshot.expiry_liquidity, today=snapshot.as_of.date()),
            width='stretch',
        )
    else:
        st.caption("아직 만기 유동성 폴링 데이터가 없습니다.")

    # 선물 시계열 범위를 옵션 차트에도 강제 적용 — 옵션은 거래가 뜸해 데이터가 1~2점뿐일 때가
    # 많은데, 그러면 Plotly가 그 점 주위로만 확대해 x축이 마이크로초 단위로 깨진다(2026-07-06 발견).
    futures_x_range = (snapshot.timestamps[0], snapshot.timestamps[-1]) if len(snapshot.timestamps) >= 2 else None

    st.subheader("Flow Radar — 옵션(가장 활발한 종목)")
    if snapshot.option_flow_symbol is not None:
        st.caption(f"종목: {snapshot.option_flow_symbol}")
        st.plotly_chart(
            build_ofi_sparkline(snapshot.option_timestamps, snapshot.option_ofi_series, x_range=futures_x_range),
            width='stretch',
        )
        st.plotly_chart(
            build_vpin_chart(snapshot.option_timestamps, snapshot.option_vpin_series, x_range=futures_x_range),
            width='stretch',
        )
        st.plotly_chart(
            build_microprice_vs_price_chart(
                snapshot.option_timestamps,
                snapshot.option_price_series,
                snapshot.option_microprice_series,
                x_range=futures_x_range,
            ),
            width='stretch',
        )
    else:
        st.caption("아직 활성 옵션 종목이 없습니다.")

    st.subheader("Flow Radar — 선물(기초자산)")
    if snapshot.futures_flow_symbol is not None:
        st.caption(f"종목: {snapshot.futures_flow_symbol}")
    st.plotly_chart(build_ofi_sparkline(snapshot.timestamps, snapshot.ofi_series), width='stretch')
    st.plotly_chart(build_vpin_chart(snapshot.timestamps, snapshot.vpin_series), width='stretch')
    st.plotly_chart(
        build_microprice_vs_price_chart(snapshot.timestamps, snapshot.price_series, snapshot.microprice_series),
        width='stretch',
    )


render()

time.sleep(REFRESH_INTERVAL_SECONDS)
st.rerun()
