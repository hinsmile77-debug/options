"""COCKPIT v1 — 관측 전용 Streamlit 대시보드 (v6 §17, PART 21 Phase1 체크리스트).

Regime · Gamma Map · Flow Radar · 수급 패널만 표시한다. 주문 실행/승인 UI(Action Feed,
원클릭 승인 등)는 COCKPIT v2(Phase3) 범위다.
"""

from __future__ import annotations

import time

import streamlit as st

from mahdi.dashboard.data_source import load_snapshot
from mahdi.dashboard.panels.flow_radar_panel import (
    build_microprice_vs_price_chart,
    build_ofi_sparkline,
    build_vpin_chart,
)
from mahdi.dashboard.panels.gamma_map_panel import build_gamma_profile_chart
from mahdi.dashboard.panels.position_panel import build_position_flow_chart
from mahdi.dashboard.panels.regime_panel import REGIME_LABEL_KO, build_regime_probability_chart

st.set_page_config(page_title="마흐디 COCKPIT v1", layout="wide")

REFRESH_INTERVAL_SECONDS = 10  # 1분봉 적재 주기보다 짧게 잡아 새 봉을 빠르게 반영


def render() -> None:
    snapshot = load_snapshot()

    st.title("마흐디 COCKPIT — 관측 전용 (Phase 1)")
    if not snapshot.is_live:
        st.warning("DB에서 데이터를 찾지 못해 합성 리플레이 데이터로 표시 중입니다 (독립 실행 모드).")

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

    st.subheader("Flow Radar")
    st.plotly_chart(build_ofi_sparkline(snapshot.timestamps, snapshot.ofi_series), width='stretch')
    st.plotly_chart(build_vpin_chart(snapshot.timestamps, snapshot.vpin_series), width='stretch')
    st.plotly_chart(
        build_microprice_vs_price_chart(snapshot.timestamps, snapshot.price_series, snapshot.microprice_series),
        width='stretch',
    )


render()

time.sleep(REFRESH_INTERVAL_SECONDS)
st.rerun()
