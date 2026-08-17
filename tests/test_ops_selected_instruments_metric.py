"""§11.5 선택기의 일일 지표 — «검정 불가한 가설»을 만들지 않기 위한 절.

`hypotheses.yaml`의 `2026-08-17-p1~p4`가 이 절을 지목한다. 지표 경로가 자동 집계에 없으면
그 가설은 **영원히 검정 불가**가 되므로(`test_ops_hypotheses.py`가 그것을 막는다), 여기서는
그 다음 단계를 고정한다: 절이 실제로 채워지는가, 그리고 컬럼이 없는 날에도 죽지 않는가.
"""

from datetime import date

import pytest

from mahdi.ops import db_metrics

TARGET = date(2026, 8, 17)


class _FakeConn:
    pass


def _patch(monkeypatch, *, one, many=None, raises=False):
    calls: list[str] = []

    def fake_fetchone(conn, sql, params=None):
        calls.append(sql)
        if raises and "selected_instruments IS NOT NULL" in sql:
            raise RuntimeError('column "selected_instruments" does not exist')
        for needle, value in one.items():
            if needle in sql:
                return value
        return None

    def fake_fetchall(conn, sql, params=None):
        calls.append(sql)
        for needle, value in (many or {}).items():
            if needle in sql:
                return value
        return []

    monkeypatch.setattr(db_metrics, "_fetchone", fake_fetchone)
    monkeypatch.setattr(db_metrics, "_fetchall", fake_fetchall)
    return calls


def test_the_section_reports_the_claim_metric_as_a_percentage(monkeypatch):
    """주장 지표는 비율이다 — 절대 건수로 주장하면 그날의 구조 변수가 분모에 숨는다(규약 F)."""
    _patch(
        monkeypatch,
        one={
            "selected_instruments IS NOT NULL": (100, 40, 36),
            "leg->>'symbol'": (50, 45, 0),
            "order_submitted": (0,),
        },
        many={"selected_instruments->>'reason'": [("no_entry_strategy", 60)]},
    )
    section = db_metrics._selected_instruments(_FakeConn(), TARGET)

    assert section["available"] is True
    assert section["enter_minutes"] == 40
    assert section["enter_minutes_with_candidate"] == 36
    assert section["enter_minutes_with_candidate_pct"] == 90.0
    assert section["symbol_resolved_pct"] == 90.0
    assert section["reason"] == {"no_entry_strategy": 60}


def test_the_two_invariants_are_counts_and_start_at_zero(monkeypatch):
    """0이어야 하는 값을 비율로 만들면 1건이 0.2%로 묻힌다 — 그래서 건수로 낸다."""
    _patch(
        monkeypatch,
        one={
            "selected_instruments IS NOT NULL": (10, 5, 5),
            "leg->>'symbol'": (5, 5, 0),
            "order_submitted": (0,),
        },
    )
    section = db_metrics._selected_instruments(_FakeConn(), TARGET)

    assert section["expiry_day_leg_count"] == 0
    assert section["order_submitted_true_count"] == 0


def test_an_expiry_day_leg_shows_up_as_a_nonzero_invariant(monkeypatch):
    """0DTE 제외가 뚫리면 이 값이 0이 아니게 된다 — 그것이 이 지표의 존재 이유다."""
    _patch(
        monkeypatch,
        one={
            "selected_instruments IS NOT NULL": (10, 5, 5),
            "leg->>'symbol'": (5, 5, 2),
            "order_submitted": (0,),
        },
    )
    assert db_metrics._selected_instruments(_FakeConn(), TARGET)["expiry_day_leg_count"] == 2


def test_a_day_without_entries_leaves_the_ratio_none_rather_than_zero(monkeypatch):
    """분모가 0인 날 0%를 찍으면 «다 실패했다»로 읽힌다 — 못 잰 것은 못 쟀다고 말한다."""
    _patch(
        monkeypatch,
        one={
            "selected_instruments IS NOT NULL": (300, 0, 0),
            "leg->>'symbol'": (0, 0, 0),
            "order_submitted": (0,),
        },
    )
    section = db_metrics._selected_instruments(_FakeConn(), TARGET)

    assert section["enter_minutes_with_candidate_pct"] is None
    assert section["symbol_resolved_pct"] is None
    assert section["recorded_minutes"] == 300


def test_a_day_before_migration_031_degrades_instead_of_dying(monkeypatch):
    """키가 사라지면 이 절을 지목한 가설이 다시 「경로 없음」이 된다 — available:False로 남긴다."""
    _patch(monkeypatch, one={}, raises=True)
    section = db_metrics._selected_instruments(_FakeConn(), TARGET)

    assert section["available"] is False
    assert "031" in section["reason"]


def test_the_section_is_reachable_from_the_decisions_axis(monkeypatch):
    """`hypotheses.yaml`이 `db.decisions.selected_instruments.…`로 지목하는 경로가 실제로 있는가."""
    _patch(
        monkeypatch,
        one={
            "count(*) FROM signal_decisions WHERE timestamp::date": (10,),
            "count(vrp)": (10,),
            "selected_instruments IS NOT NULL": (10, 2, 2),
            "leg->>'symbol'": (2, 2, 0),
            "order_submitted": (0,),
            "effective_member_count": (4.0, 3.0, 3, 1),
            "count(*) FILTER (WHERE decision='ENTER' AND timestamp::time": (0, 0, 0),
        },
    )
    monkeypatch.setattr(db_metrics, "_dead_axis_by_member", lambda conn, target: {})
    section = db_metrics.decisions(_FakeConn(), TARGET)

    assert "selected_instruments" in section
    assert section["selected_instruments"]["enter_minutes_with_candidate_pct"] == 100.0


@pytest.mark.parametrize("key", [
    "enter_minutes_with_candidate_pct", "expiry_day_leg_count",
    "order_submitted_true_count", "recorded_minutes", "symbol_resolved_pct",
])
def test_every_key_the_registered_hypotheses_point_at_exists(monkeypatch, key):
    _patch(
        monkeypatch,
        one={
            "selected_instruments IS NOT NULL": (10, 5, 5),
            "leg->>'symbol'": (5, 5, 0),
            "order_submitted": (0,),
        },
    )
    assert key in db_metrics._selected_instruments(_FakeConn(), TARGET)
