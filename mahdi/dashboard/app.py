"""COCKPIT v1 — 관측 전용 Streamlit 대시보드 (v6 §17, PART 21 Phase1 체크리스트).

Regime · Gamma Map · Flow Radar · 수급 패널만 표시한다. 주문 실행/승인 UI(Action Feed,
원클릭 승인 등)는 COCKPIT v2(Phase3) 범위다.
"""

from __future__ import annotations

import streamlit as st

from mahdi.data import db
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
    build_absorption_chart,
    build_cvd_chart,
    build_microprice_vs_price_chart,
    build_ofi_sparkline,
    build_vpin_chart,
    shared_x_range,
)
from mahdi.dashboard.panels.expiry_liquidity_panel import build_expiry_liquidity_table
from mahdi.dashboard.panels.gamma_map_panel import build_gamma_profile_chart
from mahdi.dashboard.panels.macro_panel import build_macro_snapshot_table
from mahdi.dashboard.panels.position_panel import build_position_flow_chart
from mahdi.dashboard.panels.regime_panel import REGIME_LABEL_KO, build_regime_probability_chart

_CARD_BADGE = {"ok": st.success, "warning": st.warning, "info": st.info, "neutral": st.info}

# 2026-08-05(COCKPIT 육안 점검 P2-8) — 한 줄에 놓는 배지 최대 개수.
#
# `st.columns(len(cards))`로 전부 한 줄에 펴면 개수가 늘어날수록 각 열이 좁아진다. 08-05 화면의
# "인프라" 행은 12칸이라 한 칸이 화면 폭의 8%였고, 라벨 하나("거래소 서킷브레이커/거래정지")가
# 3~4줄로 접히면서 카드 높이가 제각각이 됐다 — **"3초 룰"(스크롤 없이 한눈에)이 깨진 상태**다.
# 2026-08-01에 "인프라/관측 품질" 두 그룹으로 나눈 것이 같은 문제의 1차 대응이었는데, 그때도
# 인프라 쪽은 11칸이었고 그 뒤 매크로 신선도가 붙어 12칸이 됐다. 그룹을 더 쪼개는 대신 **행당
# 칸 수에 상한**을 둔다 — 그러면 배지가 몇 개로 늘어나도 각 칸의 폭이 더는 줄지 않는다.
#
# 6인 이유: 1920px 폭에서 한 칸이 약 300px로, 가장 긴 라벨도 2줄 안에 들어간다. 관측 품질 그룹
# (현재 7개)은 6+1로 나뉘는데, 마지막 행이 한 칸만 남더라도 `st.columns`가 폭을 균등 분할하므로
# 남은 칸이 넓어지지는 않는다(왼쪽 정렬로 자연스럽게 보인다).
_MAX_BADGES_PER_ROW = 6


def _chunked(items: list, size: int) -> list[list]:
    """items를 size개씩 끊어 리스트의 리스트로 돌려준다 — 배지 그리드 행 분할용."""
    return [items[i:i + size] for i in range(0, len(items), size)]


def _render_cards(cards: list[dict]) -> None:
    """카드 dict(label/value/status/help) 리스트를 st.columns 배지로 렌더링한다 — "3초 룰"
    (스크롤 없이 한눈에 파악)을 위해 `get_health_summary()`의 기존 배지 스타일을 그대로 재사용.
    향후 카드가 늘어나도 이 함수는 그대로고, 호출측 리스트에 dict만 추가하면 된다.
    `_MAX_BADGES_PER_ROW`를 넘으면 여러 행으로 접는다(2026-08-05 P2-8).

    열 개수를 `_MAX_BADGES_PER_ROW`로 **고정하지 않는** 이유: 카드가 그보다 적을 때 고정하면
    남는 열만큼 카드가 쪼그라든다 — 특히 계좌 폴러 미기동 시의 "계좌 현황: 아직 없음" 1장이
    화면 폭의 1/6로 찌그러진다(P2-8 최종 점검에서 실측). 상한만 두고, 그 아래면 카드 수에 맞춘다.
    """
    per_row = min(len(cards), _MAX_BADGES_PER_ROW) or 1
    for row in _chunked(cards, per_row):
        for col, card in zip(st.columns(per_row), row):
            badge = _CARD_BADGE.get(card["status"], st.info)
            with col:
                badge(f"**{card['label']}**\n\n{card['value']}")
                if card.get("help"):
                    st.caption(card["help"])

st.set_page_config(page_title="마흐디 COCKPIT v1", layout="wide")

# 2026-08-05(COCKPIT 육안 점검 P2-10) — 10초 → 30초.
#
# 종전 구조는 `render()` → `time.sleep(10)` → `st.rerun()`이었다. 두 가지 비용이 있었다.
#
# ① **데이터가 없는데도 돈다.** 이 화면의 모든 원천은 1분봉·1분 판단·5분 매크로다. 10초 주기의
#    6번 중 5번은 **직전과 완전히 같은 값**을 다시 조회해 다시 그린다(리런마다 DB 커넥션을
#    6개 넘게 새로 연다 — `db.get_connection()`은 풀이 없다).
# ② **조작이 최대 10초 늦게 먹힌다.** Streamlit은 스크립트 실행 중에 들어온 리런 요청을
#    다음 `st.*` 호출 시점에 처리하는데, `time.sleep()` 동안에는 그 지점이 없다. 슬랙 알림
#    토글을 눌러도 잠자던 10초가 끝나야 반영됐다.
#
# 30초는 1분봉 주기의 절반이라 "새 봉을 빠르게 반영한다"는 원래 성질을 유지하면서 ①을 3분의 1로
# 줄인다. ②는 주기를 늘리면 오히려 나빠지므로 **구조를 바꿔서** 없앴다 — 아래 `render()`를
# `st.fragment(run_every=...)`로 감싸 자동 갱신을 조각 안에 가두고, 슬랙 토글은 조각 **밖**
# 최상위에 두어 클릭 즉시 처리되게 했다(`time.sleep`/`st.rerun`은 제거).
REFRESH_INTERVAL_SECONDS = 30


# Absorption 캡션 — **문턱을 숫자로 적는다.** `absorption_score()`의 기본 문턱 0.05%는 가격
# 수준에 비례하는 상대값이라 상품마다 뜻이 다르다(선물 ≈11틱 vs 옵션 프리미엄은 틱보다 작음).
# 화면이 「가격 정체」라고만 쓰면 두 Radar가 같은 기준인 줄로 읽힌다.
_ABSORPTION_CAPTION = (
    "「가격 정체」 판정 문턱은 봉 내 변화율 0.05%입니다 — 가격 수준에 비례하는 상대값이라 "
    "선물(≈1,080)에서는 약 11틱까지 정체로 보지만, 옵션 프리미엄(≈16)에서는 틱 크기보다 작아 "
    "사실상 시가=종가인 봉만 통과합니다. 기준선은 직전 20봉 평균이고, 최소 5봉이 쌓이기 전 "
    "구간은 0이 아니라 **판정 불가**로 표시됩니다."
)


@st.cache_resource
def _log_cockpit_startup_once() -> None:
    # 2026-07-22(운영점검보고서 §1-1) — 이 스크립트는 REFRESH_INTERVAL_SECONDS마다 다시 실행되지만,
    # st.cache_resource로 감싸면 실제 프로세스당 딱 1회만 호출된다.
    # 좀비 프로세스(전날 떠서 안 죽고 남아있는 것)를 로그만으로 즉시 구분할 수 있게 한다.
    print(record_cockpit_startup(), flush=True)


_log_cockpit_startup_once()


def render_controls() -> None:
    """자동 갱신 조각 **밖**에서 그리는 것들 — 제목과 조작 위젯.

    2026-08-05(P2-10): 위젯을 조각 밖에 두는 것이 핵심이다. 조각 안에 있으면 `run_every` 타이머와
    사용자 클릭이 같은 실행 흐름을 공유해 조작 반영이 갱신 주기만큼 밀린다(종전 `time.sleep(10)`
    구조에서 실제로 최대 10초 지연됐다). 밖에 두면 클릭은 즉시 최상위 리런으로 처리된다.
    """
    st.title("마흐디 COCKPIT — 관측 전용 (Phase 1)")

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


@st.fragment(run_every=REFRESH_INTERVAL_SECONDS)
def render() -> None:
    """REFRESH_INTERVAL_SECONDS마다 **이 조각만** 다시 그린다(2026-08-05 P2-10).

    종전에는 스크립트 전체를 `time.sleep()` + `st.rerun()`으로 다시 돌렸다 — 조작 반영이 최대
    주기만큼 밀리고, 페이지 전체가 재구성돼 차트 줌·스크롤 위치가 매번 초기화됐다.
    """
    snapshot = load_snapshot()

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
        # 2026-08-05(P2-8): 그룹 안에서도 행당 `_MAX_BADGES_PER_ROW`개까지만 펴 각 칸의 폭을
        # 지킨다 — 08-05 인프라 12칸 화면은 한 칸이 폭의 8%라 라벨이 3~4줄로 접혔다.
        # 열 수를 그룹 크기와 상한 중 작은 쪽으로 잡아, 배지가 상한보다 적은 그룹이 쪼그라들지
        # 않게 한다(같은 그룹의 행끼리는 열 수가 같아 그리드가 어긋나지 않는다).
        per_row = min(len(checks), _MAX_BADGES_PER_ROW) or 1
        for row in _chunked(checks, per_row):
            for col, check in zip(st.columns(per_row), row):
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
    col2.metric("기초자산 현재가(지수)", f"{snapshot.spot:,.2f}")
    # 2026-08-05(P1-6) — 시각 없는 가격은 검증할 수 없다. 08-05 화면에는 지수 1,042.85와
    # 선물 1046대가 함께 떠 있었는데 어느 쪽이 언제 것인지 알 방법이 없었다. 장전에는 이 값이
    # 전일 종가인 것이 정상인데(§2 이상점 8), 그 사실도 시각이 있어야 읽힌다.
    # 2026-08-11(§3-7 / Fix#9) — **날짜가 다르면 날짜를 쓴다.**
    #
    # 08-05의 P1-6이 시각을 붙였는데 `%H:%M:%S`뿐이라, 08-11 장전 화면에 전일 종가 979.18이
    # `15:19:00 기준`으로 떴다. 시각만 있으면 "오늘 15:19"로 읽힌다 — 장전 07:30에 그 시각이
    # 아직 오지 않았다는 것을 사람이 매번 계산해야 했다.
    # **매분 날짜를 찍지는 않는다**(같은 날에는 소음이다). 다른 날일 때만 날짜와 경고를 함께 낸다.
    #
    # ⚠ 비교 대상은 `snapshot.as_of`가 **아니다.** 그 값은 `regime_state`의 최신 행 시각이라
    # (`data_source._load_from_db`), 장전에는 스팟도 레짐도 둘 다 전일 것이어서 날짜가 같아진다 —
    # 즉 가장 위험한 순간에 정확히 침묵한다. 벽시계 오늘과 비교해야 한다.
    if snapshot.spot_asof is not None:
        if snapshot.spot_asof.date() != db.local_now().date():
            col2.caption(f"⚠ **{snapshot.spot_asof:%Y-%m-%d %H:%M:%S}** 기준 — 오늘 값이 아니다")
        else:
            col2.caption(f"{snapshot.spot_asof:%H:%M:%S} 기준")
    col3.metric("레짐 안정성", "안정" if snapshot.stability_flag else "REGIME_UNSTABLE")

    st.subheader("Regime")
    # 2026-08-05(P1-7) — warmup 폴백은 확률이 아니라 one-hot 상수다. 그 사실 없이 막대만 그리면
    # "8개 중 하나를 100% 확신"으로 읽힌다(08-05 화면이 정확히 그 상태였다).
    st.plotly_chart(
        build_regime_probability_chart(snapshot.regime_prob, is_warmup=snapshot.regime_is_warmup),
        width='stretch',
    )
    if snapshot.regime_is_warmup:
        st.caption(
            "레짐 엔진이 아직 학습되지 않아 v6 §16.1 WARMUP 폴백으로 동작 중입니다 — 위 막대는 "
            "확률이 아니라 **전일 마감 레짐(또는 갭 z-score 임계 초과 시 그 방향)** 을 그대로 "
            "표시한 것입니다. 학습 진행률은 위 '레짐 엔진 학습 데이터' 배지를 보세요."
        )

    col_left, col_right = st.columns(2)
    with col_left:
        st.subheader("Gamma Map")
        strikes = [c.strike for c in snapshot.chain]
        gex = [c.gex for c in snapshot.chain]
        st.plotly_chart(
            build_gamma_profile_chart(
                strikes, gex, snapshot.spot, snapshot.gamma_flip, snapshot.gamma_walls,
                expiry=snapshot.gex_expiry,
                # 2026-08-05(P1-6) — 행사가 창은 **선물 체결가**로 굴러간다(main._roll_subscriptions_to_spot).
                # 지수 선 하나만 그리면 창이 치우쳐 보일 때 그것이 창 이동 지연인지 두 가격의 차이인지
                # 구분할 수 없다. 선물 1분봉 종가가 곧 롤링에 쓰인 그 값이다.
                futures_price=snapshot.price_series[-1] if snapshot.price_series else None,
            ),
            width='stretch',
        )
        # 2026-08-05(P0-2) — 판단 근거와 같은 북(먼슬리)만 그린다는 사실을 화면에 남긴다. 위클리
        # 북의 만기 Pinning은 별도 신호(v6 §A3)이고 여기서 합산하면 서로를 덮으므로, 여기 없다는
        # 것이 곧 "안 본다"가 아님을 함께 적어둔다(핀 리스크 패널은 아직 미구현).
        st.caption(
            "GEX·감마플립·감마월은 전부 위 만기(먼슬리) **한 북**에서만 산출합니다 — "
            "관측 루프의 진입 판단과 같은 체인입니다. 위클리 북의 만기 Pinning은 여기 포함되지 "
            "않습니다(핀 리스크 패널 미구현)."
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

    # 두 Flow Radar가 같은 x축을 쓰게 한다 — 근거는 `shared_x_range` docstring.
    flow_x_range = shared_x_range(snapshot.timestamps, snapshot.option_timestamps)

    st.subheader("Flow Radar — 옵션(가장 활발한 종목)")
    if snapshot.option_flow_symbol is not None:
        st.caption(f"종목: {snapshot.option_flow_symbol}")
        st.plotly_chart(
            build_cvd_chart(snapshot.option_timestamps, snapshot.option_cvd_series, x_range=flow_x_range),
            width='stretch',
        )
        st.plotly_chart(
            build_ofi_sparkline(snapshot.option_timestamps, snapshot.option_ofi_series, x_range=flow_x_range),
            width='stretch',
        )
        st.plotly_chart(
            build_vpin_chart(snapshot.option_timestamps, snapshot.option_vpin_series, x_range=flow_x_range),
            width='stretch',
        )
        st.plotly_chart(
            build_absorption_chart(
                snapshot.option_timestamps, snapshot.option_absorption_series, x_range=flow_x_range
            ),
            width='stretch',
        )
        st.caption(_ABSORPTION_CAPTION)
        st.plotly_chart(
            build_microprice_vs_price_chart(
                snapshot.option_timestamps,
                snapshot.option_price_series,
                snapshot.option_microprice_series,
                x_range=flow_x_range,
            ),
            width='stretch',
        )
    else:
        st.caption("아직 활성 옵션 종목이 없습니다.")

    st.subheader("Flow Radar — 선물(기초자산)")
    if snapshot.futures_flow_symbol is not None:
        st.caption(f"종목: {snapshot.futures_flow_symbol}")
    st.plotly_chart(
        build_cvd_chart(snapshot.timestamps, snapshot.cvd_series, x_range=flow_x_range), width='stretch'
    )
    st.plotly_chart(
        build_ofi_sparkline(snapshot.timestamps, snapshot.ofi_series, x_range=flow_x_range), width='stretch'
    )
    st.plotly_chart(
        build_vpin_chart(snapshot.timestamps, snapshot.vpin_series, x_range=flow_x_range), width='stretch'
    )
    st.plotly_chart(
        build_absorption_chart(snapshot.timestamps, snapshot.absorption_series, x_range=flow_x_range),
        width='stretch',
    )
    st.caption(_ABSORPTION_CAPTION)
    st.plotly_chart(
        build_microprice_vs_price_chart(
            snapshot.timestamps, snapshot.price_series, snapshot.microprice_series, x_range=flow_x_range
        ),
        width='stretch',
    )


render_controls()
# 자동 갱신은 `render`에 붙은 `st.fragment(run_every=...)`가 담당한다 —
# `time.sleep()` + `st.rerun()`은 2026-08-05(P2-10)에 제거했다(위 REFRESH_INTERVAL_SECONDS 주석).
render()
