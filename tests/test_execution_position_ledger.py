"""포지션 원장 + 브로커 대사 — 실행 배선 ①.

이 테스트가 지키는 것은 **네 가지 불변식**이다:

  1. 진입 시각을 모를 때 지어내지 않는다 — 하한을 쓰고 그 사실을 플래그로 들고 다닌다.
  2. 손익은 두 번 세어지지 않는다 — 이미 닫힌 행은 `trade_history`에 다시 안 들어간다.
  3. 방향이 뒤집히면 종료 + 개시 **두 사건**이다 — 한 행을 갈아끼우지 않는다.
  4. 유지는 로그를 안 남긴다 — 매 사이클 「아직 있다」가 사건을 덮으면 안 된다.
"""

from __future__ import annotations

import logging
from datetime import datetime

import pytest

from mahdi.execution import position_ledger as pl


def _broker(symbol: str, side: str, qty: float, avg: float, current: float | None = None):
    return pl.BrokerPosition(
        symbol=symbol, side=side, qty=qty, avg_price=avg,
        current_price=avg if current is None else current,
    )


def _entry(symbol: str, side: str = "BUY", qty: float = 1.0, avg: float = 10.0, **kw):
    kw.setdefault("opened_at", datetime(2026, 8, 24, 9, 30))
    return pl.LedgerEntry(symbol=symbol, side=side, qty=qty, entry_price=avg, **kw)


NOW = datetime(2026, 8, 24, 10, 0)
FLOOR = datetime(2026, 8, 24, 9, 55)


# ===== 불변식 1 — 모르는 진입 시각을 지어내지 않는다 =====


def test_broker_only_position_opens_as_orphan_with_floor_as_entry_time():
    result = pl.reconcile([], [_broker("B09FAWA37", "BUY", 1, 22.0)], NOW, opened_at_floor=FLOOR)

    assert len(result.opened) == 1
    entry = result.opened[0]
    assert entry.origin == pl.ORIGIN_ORPHAN
    assert entry.opened_at == FLOOR, "직전 조회 시각이 진입의 하한이다"
    assert entry.opened_at_exact is False, "하한이라는 사실이 값에 붙어 있어야 한다"
    assert result.orphans == (entry,)


def test_orphan_held_minutes_overstates_so_time_stop_fires_earlier():
    """하한을 쓰면 보유 시간은 **과대평가**된다 — 타임스톱이 더 일찍 걸리는 안전 방향이다."""
    entry = pl.reconcile(
        [], [_broker("B09FAWA37", "BUY", 1, 22.0)], NOW, opened_at_floor=FLOOR
    ).opened[0]

    # 실제 진입은 09:55~10:00 사이 어딘가다. 하한(09:55)을 쓰면 5분으로 세어진다 — 실제보다 길다.
    assert entry.held_minutes(NOW) == pytest.approx(5.0)


def test_pending_entry_context_makes_the_entry_time_exact():
    """③이 배선되면 우리 주문의 진입 맥락이 붙고, 그때는 진입 시각이 추정이 아니다."""
    context = pl.LedgerEntry(
        symbol="B09FAWA37", side="BUY", qty=1, entry_price=0.0,
        opened_at=datetime(2026, 8, 24, 9, 58, 12),
        strategy_id="vol_expansion_long", regime_entry=4, confidence_entry=0.71,
        exit_rules_key="VOL_EXPANSION", entry_order_id="0000007047",
    )

    result = pl.reconcile(
        [], [_broker("B09FAWA37", "BUY", 1, 22.0)], NOW,
        opened_at_floor=FLOOR, pending_entries={"B09FAWA37": context},
    )

    entry = result.opened[0]
    assert entry.opened_at_exact is True
    assert entry.origin == pl.ORIGIN_ORDER
    assert entry.opened_at == datetime(2026, 8, 24, 9, 58, 12)
    assert entry.strategy_id == "vol_expansion_long"
    assert entry.regime_entry == 4
    assert entry.entry_price == 22.0, "체결 평균단가는 브로커 값이 이긴다"
    assert result.orphans == ()


# ===== 유지 · 수량 변경 =====


def test_position_present_in_both_is_held_and_mirrors_broker_values():
    existing = _entry("B09FAWA37", qty=1, avg=22.0)

    result = pl.reconcile(
        [existing], [_broker("B09FAWA37", "BUY", 1, 23.5)], NOW, opened_at_floor=FLOOR
    )

    assert result.opened == () and result.closed == () and result.qty_changed == ()
    assert len(result.held) == 1
    assert result.held[0].entry_price == 23.5, "평균단가는 브로커가 진실원천이다"
    assert result.held[0].opened_at == existing.opened_at, "진입 시각은 원장이 소유한다"
    assert result.held[0].last_seen_at == NOW


@pytest.mark.parametrize(
    ("broker_qty", "increased"),
    [(3.0, True), (1.0, False)],
)
def test_qty_difference_is_reported_as_a_change_not_a_new_position(broker_qty, increased):
    existing = _entry("B09FAWA37", qty=2, avg=22.0)

    result = pl.reconcile(
        [existing], [_broker("B09FAWA37", "BUY", broker_qty, 22.0)], NOW, opened_at_floor=FLOOR
    )

    assert result.opened == () and result.closed == ()
    assert len(result.qty_changed) == 1
    change = result.qty_changed[0]
    assert (change.previous_qty, change.new_qty) == (2.0, broker_qty)
    assert change.increased is increased
    assert change.entry in result.open_entries, "수량이 변해도 여전히 열려 있다"


# ===== 종료 =====


def test_ledger_only_position_is_closed():
    existing = _entry("B09FAWA37", qty=1, avg=22.0)

    result = pl.reconcile([existing], [], NOW, opened_at_floor=FLOOR)

    assert result.open_entries == ()
    assert len(result.closed) == 1
    closed = result.closed[0]
    assert closed.entry is existing
    assert closed.exit_reason == "RECONCILED_FLAT"
    assert closed.closed_at_exact is False, "폴링 사이에 닫혔다 — 정확한 체결 시각을 모른다"


def test_gross_pnl_flips_sign_for_short_entries():
    long_leg = pl.ClosedPosition(_entry("C", "BUY", 2, 10.0), NOW, exit_price=12.0, exit_reason="TIME")
    short_leg = pl.ClosedPosition(_entry("P", "SELL", 2, 10.0), NOW, exit_price=12.0, exit_reason="TIME")

    assert long_leg.gross_pnl() == pytest.approx(4.0)
    assert short_leg.gross_pnl() == pytest.approx(-4.0)


# ===== 불변식 3 — 방향 전환은 두 사건이다 =====


def test_side_flip_closes_the_old_row_and_opens_a_new_one():
    """한 사이클 안에서 매수 청산 + 매도 진입이 일어난 경우.

    한 행의 `side`만 갈아끼우면 **매수 트레이드의 손익이 통째로 사라진다.**
    """
    existing = _entry("B09FAWA37", side="BUY", qty=1, avg=22.0)

    result = pl.reconcile(
        [existing], [_broker("B09FAWA37", "SELL", 1, 19.0, current=19.0)], NOW,
        opened_at_floor=FLOOR,
    )

    assert len(result.closed) == 1
    assert result.closed[0].entry.side == "BUY"
    assert result.closed[0].exit_reason == "SIDE_FLIPPED"
    assert len(result.opened) == 1
    assert result.opened[0].side == "SELL"


# ===== trade_history 변환 — 모르는 값을 0으로 채우지 않는다 =====


def test_trade_history_row_leaves_unknown_costs_as_none():
    closed = pl.ClosedPosition(
        _entry("B09FAWA37", "BUY", 1, 22.0, strategy_id="s", regime_entry=4, confidence_entry=0.71),
        datetime(2026, 8, 24, 10, 30), exit_price=25.0, exit_reason="TIME",
    )

    row = pl.trade_history_row(closed)

    assert row["gross_pnl"] == pytest.approx(3.0)
    # 0.0으로 채우면 「비용이 없었다」가 되고, 이 표로 학습한 모델이 무비용 거래를 배운다.
    assert row["commission"] is None
    assert row["slippage"] is None
    assert row["net_pnl"] is None
    assert row["regime_entry"] == 4
    assert row["confidence_entry"] == 0.71


# ===== 원장 행 왕복 =====


def test_ledger_row_and_entry_from_row_round_trip():
    entry = _entry(
        "B09FAWA37", "SELL", 2, 15.5, opened_at_exact=False, origin=pl.ORIGIN_ORPHAN,
        strategy_id="short_gamma", regime_entry=5, exit_rules_key="VOL_EXPANSION",
        confidence_entry=0.66, entry_order_id="0000007047", last_seen_at=NOW,
    )

    assert pl.entry_from_row(pl.ledger_row(entry)) == entry


# ===== 불변식 2 · 4 — 적재 순서와 로그 볼륨 =====


class _FakeConn:
    """`db` 호출을 가로채는 최소 스텁 — 순서와 짝만 검사한다."""

    def __init__(self, close_returns: list[bool] | None = None):
        self.upserts: list[dict] = []
        self.closes: list[dict] = []
        self.trades: list[dict] = []
        self._close_returns = close_returns or []


@pytest.fixture
def fake_db(monkeypatch):
    conn = _FakeConn()

    def _upsert(_conn, row):
        conn.upserts.append(row)

    def _close(_conn, *, symbol, opened_at, closed_at, exit_price, exit_reason):
        conn.closes.append({"symbol": symbol, "opened_at": opened_at, "reason": exit_reason})
        return conn._close_returns.pop(0) if conn._close_returns else True

    def _insert_trade(_conn, row):
        conn.trades.append(row)

    monkeypatch.setattr(pl.db, "upsert_position_ledger", _upsert)
    monkeypatch.setattr(pl.db, "close_position_ledger", _close)
    monkeypatch.setattr(pl.db, "insert_trade_history", _insert_trade)
    return conn


def test_apply_writes_trade_history_only_for_rows_it_actually_closed(fake_db):
    """같은 종료를 두 번 보고해도 손익은 한 번만 세어진다.

    두 번째 `close_position_ledger()`는 `closed_at IS NULL` 조건에 걸려 False를 돌려주고,
    그때 `trade_history` 적재를 건너뛰는 것이 이 짝의 존재 이유다.
    """
    fake_db._close_returns = [True, False]
    result = pl.ReconcileResult(
        closed=(
            pl.ClosedPosition(_entry("A"), NOW, exit_price=11.0, exit_reason="TIME"),
            pl.ClosedPosition(_entry("B"), NOW, exit_price=9.0, exit_reason="TIME"),
        )
    )

    stats = pl.apply_reconcile(fake_db, result)

    assert stats == {"upserted": 0, "closed": 1, "trades": 1, "already_closed": 1}
    assert len(fake_db.trades) == 1
    assert fake_db.trades[0]["symbol"] == "A"


def test_apply_upserts_every_open_entry(fake_db):
    result = pl.ReconcileResult(
        opened=(_entry("A"),),
        held=(_entry("B"),),
        qty_changed=(pl.QtyChange(_entry("C"), 1.0, 2.0, NOW),),
    )

    stats = pl.apply_reconcile(fake_db, result)

    assert stats["upserted"] == 3
    assert {r["symbol"] for r in fake_db.upserts} == {"A", "B", "C"}


def test_held_positions_produce_no_log_lines(caplog):
    """유지는 사건이 아니다 — 매 사이클 찍으면 그 줄들이 진짜 사건을 덮는다."""
    result = pl.ReconcileResult(held=(_entry("A"), _entry("B")))

    with caplog.at_level(logging.INFO, logger="mahdi.execution.position_ledger"):
        pl.log_reconcile(result, NOW)

    assert caplog.records == []


def test_orphan_logs_a_warning_and_not_a_duplicate_open_line(caplog):
    result = pl.reconcile([], [_broker("A", "BUY", 1, 22.0)], NOW, opened_at_floor=FLOOR)

    with caplog.at_level(logging.INFO, logger="mahdi.execution.position_ledger"):
        pl.log_reconcile(result, NOW)

    assert len(caplog.records) == 1, "고아는 경고 한 줄이지 개시 줄까지 두 줄이 아니다"
    assert caplog.records[0].levelno == logging.WARNING


# ===== 요약 — 규약 C(셋을 가른다) =====


def test_summary_counts_unknown_entry_time_separately():
    entries = (
        _entry("A"),
        _entry("B", opened_at_exact=False, origin=pl.ORIGIN_ORPHAN),
    )
    result = pl.ReconcileResult(opened=(entries[1],), held=(entries[0],))

    summary = pl.LedgerSummary.from_entries(entries, result)

    assert summary.open_count == 2
    assert summary.orphan_count == 1
    assert summary.unknown_entry_time == 1
    assert summary.opened_today == 1
    assert summary.closed_today == 0

