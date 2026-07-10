from datetime import date

from mahdi.dashboard.panels.expiry_liquidity_panel import build_expiry_liquidity_table


def test_build_expiry_liquidity_table_formats_pct_spread_not_dollar():
    rows = [
        {
            "series": "regular",
            "expiry": date(2026, 7, 30),
            "atm_spread_pct": 0.041,
            "depth": 220.0,
            "volume": 480.0,
            "days_to_expiry": 24,
        },
        {
            "series": "weekly",
            "expiry": date(2026, 7, 9),
            "atm_spread_pct": 0.093,
            "depth": 70.0,
            "volume": 140.0,
            "days_to_expiry": 3,
        },
    ]

    fig = build_expiry_liquidity_table(rows)

    table = fig.data[0]
    labels, expiries, spreads, depths, volumes, days = table.cells.values
    assert list(labels) == ["먼슬리", "위클리"]
    assert list(expiries) == ["2026-07-30", "2026-07-09"]
    assert list(spreads) == ["4.10%", "9.30%"]  # Cao-Wei %스프레드 — 달러 스프레드 아님
    assert list(depths) == ["220", "70"]
    assert list(volumes) == ["480", "140"]
    assert list(days) == ["24", "3"]


def test_build_expiry_liquidity_table_handles_missing_values():
    rows = [{"series": "weekly", "expiry": None, "atm_spread_pct": None, "depth": None, "volume": None, "days_to_expiry": None}]

    fig = build_expiry_liquidity_table(rows)

    table = fig.data[0]
    labels, expiries, spreads, depths, volumes, days = table.cells.values
    assert list(labels) == ["위클리"]
    assert list(expiries) == ["-"]
    assert list(spreads) == ["-"]


def test_build_expiry_liquidity_table_notes_monthly_expiry_week():
    # regular 만기가 today와 같은 ISO주(2026-07-09, 목)에 속함 — 이번 주가 먼슬리 만기 주라서
    # 위클리 신규 상장이 없는 경우(2026-07-10 실측 근거)를 사람이 바로 알아볼 수 있어야 한다.
    rows = [
        {
            "series": "regular",
            "expiry": date(2026, 7, 9),
            "atm_spread_pct": 0.02,
            "depth": 300.0,
            "volume": 900.0,
            "days_to_expiry": 0,
        },
        {
            "series": "weekly",
            "expiry": date(2026, 7, 16),  # 이번 주가 아니라 차주 위클리
            "atm_spread_pct": 0.05,
            "depth": 90.0,
            "volume": 200.0,
            "days_to_expiry": 7,
        },
    ]

    fig = build_expiry_liquidity_table(rows, today=date(2026, 7, 8))

    assert "먼슬리 만기 주" in fig.layout.title.text


def test_build_expiry_liquidity_table_no_note_outside_monthly_expiry_week():
    rows = [
        {
            "series": "regular",
            "expiry": date(2026, 8, 13),
            "atm_spread_pct": 0.02,
            "depth": 300.0,
            "volume": 900.0,
            "days_to_expiry": 34,
        },
        {
            "series": "weekly",
            "expiry": date(2026, 7, 16),
            "atm_spread_pct": 0.05,
            "depth": 90.0,
            "volume": 200.0,
            "days_to_expiry": 7,
        },
    ]

    fig = build_expiry_liquidity_table(rows, today=date(2026, 7, 8))

    assert fig.layout.title.text is None
