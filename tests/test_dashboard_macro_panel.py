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
    vix_front, vix_next, term_structure, usdcnh, zn_front, us10y = table.cells.values
    assert vix_front == ["17.50"]
    assert vix_next == ["17.80"]
    assert "콘탱고" in term_structure[0]
    assert term_structure[0].startswith("+")
    assert usdcnh == ["6.7803"]
    assert zn_front == ["110.2500"]
    assert us10y == ["4.54%"]


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
    assert values == ["-", "-", "-", "-", "-", "-"]


def test_build_macro_snapshot_table_handles_missing_us10y_only():
    # CBOT 미신청 계좌라 US10Y/ZN이 아직 안 채워진 상태(정상) — 나머지 필드는 그대로 보여야 한다.
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
