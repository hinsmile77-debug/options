"""진입 판단의 사후 평가 (2026-08-06 고도화#5).

08-05 `p1`이 팔레트를 연 뒤 ENTER가 0 → 62건이 됐는데 **그 62건을 재는 축이 하나도 없었다.**
여기서 고정하는 것은 계산식이 아니라 **판정 규약**이다 — 특히 "무변동은 적중이 아니다".
"""

from __future__ import annotations

from datetime import date

import pytest

from mahdi.ops import decision_outcomes


def test_horizons_are_plural_so_a_lucky_window_cannot_pass_alone():
    """지평이 하나면 그 지평에 우연히 맞은 것과 구분이 안 된다."""
    assert len(decision_outcomes.HORIZON_MINUTES) >= 3
    assert decision_outcomes.HORIZON_MINUTES == tuple(sorted(decision_outcomes.HORIZON_MINUTES))


# ===== 적중 판정 규약 =====
#
# SQL 식이라 파이썬으로 직접 못 부른다 — **식이 실제로 낼 답을 여기 표로 고정**하고,
# `_hit_expr()`가 그 표대로 쓰였는지 문자열로 확인한다.


def _hit(direction: float | None, entry: float | None, later: float | None) -> bool | None:
    """`_hit_expr()`가 SQL에서 내는 것과 같은 판정을 파이썬으로 옮긴 참조 구현."""
    if direction is None or entry is None or later is None:
        return None
    if direction == 0:
        return None
    if later == entry:
        return None
    return (direction * (later - entry)) > 0


@pytest.mark.parametrize(
    "direction, entry, later, expected",
    [
        (+1.0, 1000.0, 1002.0, True),    # 위라고 했고 올랐다
        (+1.0, 1000.0, 998.0, False),    # 위라고 했는데 내렸다
        (-1.0, 1000.0, 998.0, True),     # 아래라고 했고 내렸다
        (-1.0, 1000.0, 1002.0, False),
        (+0.3, 1000.0, 1000.5, True),    # 방향의 크기는 판정에 안 쓴다 — 부호만 본다
        (+1.0, 1000.0, 1000.0, None),    # **무변동은 적중도 실패도 아니다**
        (0.0, 1000.0, 1002.0, None),     # 방향이 중립이면 판정 대상이 아니다
        (+1.0, None, 1002.0, None),      # 진입 스팟 결손
        (+1.0, 1000.0, None, None),      # 지평 미충족(장 마감을 넘겼다)
    ],
)
def test_hit_rules(direction, entry, later, expected):
    assert _hit(direction, entry, later) is expected


def test_sql_expression_encodes_those_same_rules():
    """참조 구현과 SQL이 갈라지면 이 테스트가 무의미해진다 — 식의 뼈대를 확인한다."""
    expr = decision_outcomes._hit_expr("spot_5")
    assert "direction = 0 THEN NULL" in expr          # 중립 방향
    assert "spot_5 = entry_spot THEN NULL" in expr    # 무변동
    assert "(direction * (spot_5 - entry_spot)) > 0" in expr


# ===== 집계 =====


class _FakeCursor:
    def __init__(self, row):
        self._row = row
        self.executed: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, row):
        self._cursor = _FakeCursor(row)

    def cursor(self):
        return self._cursor

    def rollback(self):
        pass


def test_summarize_reports_sample_size_alongside_the_rate():
    """**표본 수를 반드시 함께 낸다** — 진입 3건인 날의 100%는 아무 뜻이 없다."""
    # (entries, n_5, hit_5, n_15, hit_15, n_30, hit_30, mv_5, mv_15, mv_30) — 08-06 실측값
    # + 2026-08-11 고도화 C의 |이동폭| 세 열(비율이라 0.00123 = 0.123%).
    conn = _FakeConn((62, 57, 28, 61, 37, 47, 37, 0.00123, 0.00250, 0.00410))
    result = decision_outcomes.summarize(conn, date(2026, 8, 6))
    assert result["entries"] == 62
    assert result["horizons"]["5m"] == {
        "sample": 57, "hits": 28, "hit_pct": 49.1, "abs_move_pct": 0.123
    }
    assert result["horizons"]["30m"]["abs_move_pct"] == 0.41


def test_absolute_move_is_reported_even_when_direction_hits_are_meaningless():
    """고도화 C — 방향 중립 전략(`straddle_accumulate`)에는 적중률이 틀린 질문이다.

    08-11에 ENTER 281건이 전부 스트래들이었는데 적중률만 있었다. 이동폭은 별개 축이라
    적중이 0건이어도 값이 나와야 한다.
    """
    conn = _FakeConn((10, 10, 0, 10, 0, 10, 0, 0.005, 0.008, 0.011))
    result = decision_outcomes.summarize(conn, date(2026, 8, 11))
    assert result["horizons"]["15m"]["hit_pct"] == 0.0
    assert result["horizons"]["15m"]["abs_move_pct"] == 0.8


def test_a_horizon_with_no_sample_reports_none_not_zero():
    """0%와 "잴 게 없었다"는 다르다 — 0으로 내면 마지막 30분 진입이 성과를 끌어내린다."""
    conn = _FakeConn((5, 0, 0, 0, 0, 0, 0, None, None, None))
    result = decision_outcomes.summarize(conn, date(2026, 8, 6))
    assert result["horizons"]["5m"]["hit_pct"] is None
    # 이동폭도 같은 규칙 — 표본이 없으면 0이 아니라 None이다.
    assert result["horizons"]["5m"]["abs_move_pct"] is None


def test_a_day_without_entries_is_not_available_rather_than_zero():
    conn = _FakeConn((0, 0, 0, 0, 0, 0, 0, None, None, None))
    assert decision_outcomes.summarize(conn, date(2026, 8, 6))["available"] is False


def test_summarize_survives_a_missing_table():
    class _Broken(_FakeConn):
        def cursor(self):
            raise RuntimeError("relation decision_outcomes does not exist")

    assert decision_outcomes.summarize(_Broken(None), date(2026, 8, 6)) == {"available": False}


def test_compute_survives_a_missing_table():
    """마이그레이션 027 적용 전에도 장마감 배치의 나머지 흐름을 막지 않는다."""

    class _Broken:
        def cursor(self):
            raise RuntimeError("relation decision_outcomes does not exist")

        def rollback(self):
            pass

    assert decision_outcomes.compute(_Broken(), date(2026, 8, 6)) == 0
