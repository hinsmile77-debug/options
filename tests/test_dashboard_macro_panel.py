from mahdi.dashboard.panels.macro_panel import build_macro_snapshot_table


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

    table = fig.data[0]
    vix_front, vix_next, term_structure, usdcnh, zn_front, us10y, usdkrw, es_front, move_index = table.cells.values
    assert vix_front == ["17.50"]
    assert vix_next == ["17.80"]
    assert "콘탱고" in term_structure[0]
    assert term_structure[0].startswith("+")
    assert usdcnh == ["6.7803"]
    assert zn_front == ["110.2500"]
    assert us10y == ["4.54%"]
    assert usdkrw == ["-"]
    assert es_front == ["-"]
    assert move_index == ["-"]


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

    term_structure = fig.data[0].cells.values[2][0]
    assert "백워데이션" in term_structure
    assert term_structure.startswith("-")


def test_build_macro_snapshot_table_handles_none_snapshot():
    fig = build_macro_snapshot_table(None)

    values = [v[0] for v in fig.data[0].cells.values]
    assert values == ["-", "-", "-", "-", "-", "-", "-", "-", "-"]


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

    values = [v[0] for v in fig.data[0].cells.values]
    assert values[0] == "17.50"
    assert values[4] == "-"  # zn_front
    assert values[5] == "-"  # us10y_yield


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

    zn_front = fig.data[0].cells.values[4][0]
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

    zn_front = fig.data[0].cells.values[4][0]
    assert zn_front == "108.5000 (폴백)"


def test_build_macro_snapshot_table_shows_usdkrw_daily_level():
    # 2026-07-20 추가 — USDKRW는 US10Y와 동일하게 계좌 게이트 없는 무료 일봉 경로.
    snapshot = {"usdkrw": 1352.30}

    fig = build_macro_snapshot_table(snapshot)

    usdkrw = fig.data[0].cells.values[6][0]
    assert usdkrw == "1352.30"


def test_build_macro_snapshot_table_shows_es_front_from_kis():
    snapshot = {"es_front": 5123.25, "es_front_source": "kis"}

    fig = build_macro_snapshot_table(snapshot)

    es_front = fig.data[0].cells.values[7][0]
    assert es_front == "5123.2500"


def test_build_macro_snapshot_table_labels_yfinance_fallback_es_front():
    # ES(CME E-mini S&P500)도 ZN과 동일하게 KIS 유료 항목이라 미구독 상태에서는 폴백값이 온다.
    snapshot = {"es_front": 5100.00, "es_front_source": "yfinance_fallback"}

    fig = build_macro_snapshot_table(snapshot)

    es_front = fig.data[0].cells.values[7][0]
    assert es_front == "5100.0000 (폴백)"


def test_build_macro_snapshot_table_labels_move_index_as_fallback():
    # MOVE는 장외 인덱스라 KIS 경로가 없어 항상 yfinance_fallback에서만 온다.
    snapshot = {"move_index": 95.30, "move_index_source": "yfinance_fallback"}

    fig = build_macro_snapshot_table(snapshot)

    move_index = fig.data[0].cells.values[8][0]
    assert move_index == "95.30 (폴백)"
