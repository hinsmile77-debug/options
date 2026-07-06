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
