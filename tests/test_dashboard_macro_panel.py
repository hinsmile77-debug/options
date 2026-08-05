from datetime import datetime

from mahdi.dashboard.panels.macro_panel import build_macro_snapshot_table

# 2026-08-05(P1-4): 표 맨 앞에 "기준 시각" 열이 추가돼 셀 인덱스가 1씩 밀렸다. 인덱스를 숫자로
# 흩어놓으면 열이 또 바뀔 때 전부 손대야 하므로 이름으로 고정한다.
_COL = {
    "as_of": 0, "vix_front": 1, "vix_next": 2, "term_structure": 3, "usdcnh": 4,
    "zn_front": 5, "us10y": 6, "usdkrw": 7, "es_front": 8, "move_index": 9,
}


def _cell(fig, name: str) -> str:
    return fig.data[0].cells.values[_COL[name]][0]


def test_build_macro_snapshot_table_shows_contango_when_next_above_front():
    snapshot = {
        "vix_front": 17.50,
        "vix_next": 17.80,
        "vix_term_structure": 17.80 / 17.50 - 1,
        "usdcnh": 6.7803,
        "us10y_yield": 4.54,
        "zn_front": 110.25,
    }

    fig = build_macro_snapshot_table(snapshot)

    assert _cell(fig, "vix_front") == "17.50"
    assert _cell(fig, "vix_next") == "17.80"
    assert "콘탱고" in _cell(fig, "term_structure")
    assert _cell(fig, "term_structure").startswith("+")
    assert _cell(fig, "usdcnh") == "6.7803"
    assert _cell(fig, "zn_front") == "110.2500"
    assert _cell(fig, "us10y") == "4.54%"
    assert _cell(fig, "usdkrw") == "-"
    assert _cell(fig, "es_front") == "-"
    assert _cell(fig, "move_index") == "-"


def test_build_macro_snapshot_table_shows_backwardation_when_next_below_front():
    snapshot = {
        "vix_front": 25.0,
        "vix_next": 22.0,
        "vix_term_structure": 22.0 / 25.0 - 1,
        "usdcnh": 7.10,
        "us10y_yield": 4.20,
        "zn_front": 108.50,
    }

    fig = build_macro_snapshot_table(snapshot)

    term_structure = _cell(fig, "term_structure")
    assert "백워데이션" in term_structure
    assert term_structure.startswith("-")


def test_build_macro_snapshot_table_handles_none_snapshot():
    fig = build_macro_snapshot_table(None)

    values = [v[0] for v in fig.data[0].cells.values]
    assert values == ["-"] * 10  # 기준 시각 열이 앞에 추가돼 10칸


def test_build_macro_snapshot_table_handles_missing_us10y_only():
    # CBOT 미구독 계좌라 US10Y/ZN이 아직 안 채워진 상태(정상) — 나머지 필드는 그대로 보여야 한다.
    snapshot = {
        "vix_front": 17.50,
        "vix_next": 17.80,
        "vix_term_structure": 0.017143,
        "usdcnh": 6.7803,
        "us10y_yield": None,
        "zn_front": None,
    }

    fig = build_macro_snapshot_table(snapshot)

    assert _cell(fig, "vix_front") == "17.50"
    assert _cell(fig, "zn_front") == "-"
    assert _cell(fig, "us10y") == "-"


def test_build_macro_snapshot_table_shows_zn_front_when_cbot_enabled():
    snapshot = {
        "vix_front": 17.50,
        "vix_next": 17.80,
        "vix_term_structure": 0.017143,
        "usdcnh": 6.7803,
        "us10y_yield": 4.54,
        "zn_front": 110.25,
    }

    fig = build_macro_snapshot_table(snapshot)

    zn_front = _cell(fig, "zn_front")
    assert zn_front == "110.2500"


def test_build_macro_snapshot_table_labels_yfinance_fallback_zn_front():
    # 2026-07-20: CME|CBOT가 KIS 유료 항목(월 228.8불)이라 미구독 상태일 때 zn_front가
    # yfinance 폴백값이면(mahdi/data/yfinance_fallback.py) 실제 CBOT 체결가와 구분되도록 표시해야 한다.
    snapshot = {
        "vix_front": 17.50,
        "vix_next": 17.80,
        "vix_term_structure": 0.017143,
        "usdcnh": 6.7803,
        "us10y_yield": 4.54,
        "zn_front": 108.50,
        "zn_front_source": "yfinance_fallback",
    }

    fig = build_macro_snapshot_table(snapshot)

    zn_front = _cell(fig, "zn_front")
    assert zn_front == "108.5000 (폴백)"


def test_build_macro_snapshot_table_shows_usdkrw_daily_level():
    # 2026-07-20 추가 — USDKRW는 US10Y와 동일하게 계좌 게이트 없는 무료 일봉 경로.
    snapshot = {"usdkrw": 1352.30}

    fig = build_macro_snapshot_table(snapshot)

    usdkrw = _cell(fig, "usdkrw")
    assert usdkrw == "1352.30"


def test_build_macro_snapshot_table_shows_es_front_from_kis():
    snapshot = {"es_front": 5123.25, "es_front_source": "kis"}

    fig = build_macro_snapshot_table(snapshot)

    es_front = _cell(fig, "es_front")
    assert es_front == "5123.2500"


def test_build_macro_snapshot_table_labels_yfinance_fallback_es_front():
    # ES(CME E-mini S&P500)도 ZN과 동일하게 KIS 유료 항목이라 미구독 상태에서는 폴백값이 온다.
    snapshot = {"es_front": 5100.00, "es_front_source": "yfinance_fallback"}

    fig = build_macro_snapshot_table(snapshot)

    es_front = _cell(fig, "es_front")
    assert es_front == "5100.0000 (폴백)"


def test_build_macro_snapshot_table_labels_move_index_as_fallback():
    # MOVE는 장외 인덱스라 KIS 경로가 없어 항상 yfinance_fallback에서만 온다.
    snapshot = {"move_index": 95.30, "move_index_source": "yfinance_fallback"}

    fig = build_macro_snapshot_table(snapshot)

    move_index = _cell(fig, "move_index")
    assert move_index == "95.30 (폴백)"


# ===== 2026-08-05 P1-4: 표에 시각이 하나도 없었다 =====


def test_build_macro_snapshot_table_shows_the_snapshot_timestamp():
    """폴러가 죽으면 며칠 전 값이 "지금"으로 보였다 — 기준 시각이 없으면 알 방법이 없다."""
    snapshot = {"timestamp": datetime(2026, 8, 5, 12, 10), "vix_front": 17.90}

    fig = build_macro_snapshot_table(snapshot)

    assert _cell(fig, "as_of") == "08-05 12:10"


def test_build_macro_snapshot_table_marks_values_carried_from_an_earlier_day():
    """LOCF로 실려온 값은 날짜가 다르면 관측 날짜를 함께 쓴다 — 일봉 항목은 전 거래일 값이 정상이지만
    그 사실이 화면에 보여야 한다."""
    snapshot = {
        "timestamp": datetime(2026, 8, 5, 12, 10),
        "us10y_yield": 4.63,
        "us10y_yield_asof": datetime(2026, 8, 4, 15, 40),
        "move_index": 77.56,
        "move_index_source": "yfinance_fallback",
        "move_index_asof": datetime(2026, 7, 31, 9, 0),
    }

    fig = build_macro_snapshot_table(snapshot)

    assert _cell(fig, "us10y") == "4.63% (08-04)"
    assert _cell(fig, "move_index") == "77.56 (폴백) (07-31)"


def test_build_macro_snapshot_table_does_not_mark_same_day_carry_forward():
    # 같은 날 안에서의 이월(일봉 항목 6시간 주기 등)은 정상 — 전부 표기하면 정작 며칠 전 값이 안 띈다.
    snapshot = {
        "timestamp": datetime(2026, 8, 5, 12, 10),
        "us10y_yield": 4.63,
        "us10y_yield_asof": datetime(2026, 8, 5, 7, 35),
    }

    fig = build_macro_snapshot_table(snapshot)

    assert _cell(fig, "us10y") == "4.63%"
