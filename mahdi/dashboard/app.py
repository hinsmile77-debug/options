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
from mahdi.dashboard.panels.expiry_liquidity_panel import build_expiry_liquidity_table
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
