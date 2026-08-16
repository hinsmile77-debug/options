from datetime import datetime

import pytest

from mahdi.execution.account_tracker import (
    BalanceSnapshot,
    build_account_state,
    parse_balance_response,
    snapshot_to_row,
)

_TS = datetime(2026, 7, 28, 10, 0)

_RESPONSE = {
    "rt_cd": "0",
    "output1": [
        {"shtn_pdno": "101S03", "sll_buy_dvsn_name": "BUY"},
        {"shtn_pdno": "201S03C325", "sll_buy_dvsn_name": "SLL"},
        {"shtn_pdno": "201S03C330", "sll_buy_dvsn_name": "SLL"},
        {"shtn_pdno": "flattened", "sll_buy_dvsn_name": ""},  # 당일 청산돼 빈칸 -> 어느 쪽도 아님
    ],
    "output2": {
        "prsm_dpast": "50000000",
        "evlu_pfls_amt_smtl": "16125000",
        "trad_pfls_amt_smtl": "0",
        "dnca_cash": "50000000",
        "ord_psbl_cash": "48000000",
        "mgna_tota": "2000000",
    },
}


def test_parse_balance_response_extracts_pnl_fields_and_position_counts():
    snapshot = parse_balance_response(_RESPONSE, _TS)

    assert snapshot.prsm_dpast == 50000000.0
    assert snapshot.evlu_pfls_amt_smtl == 16125000.0
    assert snapshot.dnca_cash == 50000000.0
    assert snapshot.ord_psbl_cash == 48000000.0
    assert snapshot.same_direction_buy_count == 1
    assert snapshot.same_direction_sell_count == 2


def test_parse_balance_response_missing_output2_raises():
    with pytest.raises(ValueError):
        parse_balance_response({"output1": []}, _TS)


def test_parse_balance_response_missing_output1_means_no_positions():
    snapshot = parse_balance_response({"output2": {"prsm_dpast": "1000"}}, _TS)
    assert snapshot.same_direction_buy_count == 0
    assert snapshot.same_direction_sell_count == 0


def test_parse_balance_response_treats_blank_or_none_numeric_fields_as_zero():
    snapshot = parse_balance_response({"output2": {"prsm_dpast": None, "dnca_cash": ""}}, _TS)
    assert snapshot.prsm_dpast == 0.0
    assert snapshot.dnca_cash == 0.0


def test_snapshot_to_row_round_trips_all_fields():
    snapshot = parse_balance_response(_RESPONSE, _TS)
    row = snapshot_to_row(snapshot)
    assert row["timestamp"] == _TS
    assert row["prsm_dpast"] == 50000000.0
    assert row["same_direction_buy_count"] == 1
    assert row["same_direction_sell_count"] == 2


def _snapshot(prsm_dpast: float, buy=0, sell=0) -> BalanceSnapshot:
    return BalanceSnapshot(
        timestamp=_TS, prsm_dpast=prsm_dpast, evlu_pfls_amt_smtl=0.0, trad_pfls_amt_smtl=0.0,
        dnca_cash=0.0, ord_psbl_cash=0.0, mgna_tota=0.0,
        same_direction_buy_count=buy, same_direction_sell_count=sell,
    )


def test_build_account_state_computes_daily_and_weekly_pnl_pct():
    latest = _snapshot(110.0)
    start_of_day = _snapshot(100.0)
    start_of_week = _snapshot(80.0)

    state = build_account_state(
        latest, start_of_day, start_of_week, peak_prsm_dpast=110.0,
        candidate_side="BUY", daily_trades_by_strategy={},
    )

    assert state.daily_pnl_pct == pytest.approx(0.10)
    assert state.weekly_pnl_pct == pytest.approx(0.375)
    assert state.drawdown_pct == pytest.approx(0.0)  # latest == peak


def test_build_account_state_drawdown_negative_when_below_peak():
    latest = _snapshot(90.0)
    state = build_account_state(
        latest, start_of_day=None, start_of_week=None, peak_prsm_dpast=100.0,
        candidate_side="BUY", daily_trades_by_strategy={},
    )
    assert state.drawdown_pct == pytest.approx(-0.10)


def test_build_account_state_missing_baselines_fall_back_to_zero():
    latest = _snapshot(100.0)
    state = build_account_state(
        latest, start_of_day=None, start_of_week=None, peak_prsm_dpast=None,
        candidate_side="BUY", daily_trades_by_strategy={},
    )
    assert state.daily_pnl_pct == 0.0
    assert state.weekly_pnl_pct == 0.0
    assert state.drawdown_pct == 0.0


def test_build_account_state_same_direction_positions_matches_candidate_side():
    latest = _snapshot(100.0, buy=2, sell=5)
    buy_state = build_account_state(
        latest, None, None, None, candidate_side="BUY", daily_trades_by_strategy={}
    )
    sell_state = build_account_state(
        latest, None, None, None, candidate_side="SELL", daily_trades_by_strategy={}
    )
    assert buy_state.same_direction_positions == 2
    assert sell_state.same_direction_positions == 5


def test_build_account_state_passes_through_daily_trades_and_pending_loss():
    latest = _snapshot(100.0)
    state = build_account_state(
        latest, None, None, None, candidate_side="BUY",
        daily_trades_by_strategy={"vrp_harvest": 2}, pending_trade_loss_pct=-0.01,
    )
    assert state.daily_trades_by_strategy == {"vrp_harvest": 2}
    assert state.pending_trade_loss_pct == -0.01


# ===== 2026-08-16 (Block B) — 방향 판정과 포지션 레코드 =====
#
# 이 절이 지키는 것은 하나다: **방향을 못 읽었을 때 조용히 0이 되지 않는다.**
# 조용히 0이 되면 동일방향 한도(risk/limits.py)와 물타기 금지(execution/entry.py)가
# **둘 다** 무력화되는데, 로그에는 아무 흔적이 없다(계명 12).

from mahdi.execution.account_tracker import (  # noqa: E402
    SIDE_BUY,
    SIDE_SELL,
    SIDE_UNKNOWN,
    classify_side,
    has_open_position_same_direction,
    position_rows,
    same_direction_positions,
)


def test_classify_side_accepts_the_korean_forms_the_official_doc_documents():
    """공식 문서(`선물옵션 잔고현황` 시트)는 `sll_buy_dvsn_name`을 이렇게 적는다:

        매수잔고인 경우, "매수" 혹은 "BUY"로 출력

    종전 구현은 "BUY"/"SLL" **두 리터럴만** 봤다. 이 계좌는 포지션을 가진 적이 없어
    (`execution_logs` 0행) 실제 값은 **미실측**이다 — 한글로 오면 카운트가 0이 됐다.
    """
    assert classify_side("BUY") == SIDE_BUY
    assert classify_side("매수") == SIDE_BUY
    assert classify_side("SLL") == SIDE_SELL
    assert classify_side("매도") == SIDE_SELL
    assert classify_side("sell") == SIDE_SELL  # 대소문자·공백에 걸리지 않는다
    assert classify_side(" BUY ") == SIDE_BUY


def test_classify_side_falls_back_to_the_code_field_then_gives_up_loudly():
    """이름값이 비면 `sll_buy_dvsn_cd`를 본다(같은 문서에 Required로 있다).
    그래도 모르면 **UNKNOWN** — 매수도 매도도 아닌 제3의 상태로 드러낸다."""
    assert classify_side("", "02") == SIDE_BUY
    assert classify_side(None, "01") == SIDE_SELL
    # 이름값이 이긴다 — 코드값 매핑(01/02)은 아직 실측되지 않은 관례다.
    assert classify_side("매수", "01") == SIDE_BUY
    assert classify_side("처음보는값") == SIDE_UNKNOWN
    assert classify_side(None, None) == SIDE_UNKNOWN


def test_unrecognised_side_is_counted_and_warned_not_silently_dropped(caplog):
    """**이 테스트가 Block B의 핵심이다.**

    모르는 방향값을 만나면 (a) `unknown_side_count`로 세고 (b) 원본 값을 로그에 남긴다.
    8/18에 첫 포지션이 생기는 날 그 로그 한 줄이 실측값을 알려준다.
    """
    response = {
        "output1": [
            {"shtn_pdno": "101S03", "sll_buy_dvsn_name": "롱", "cblc_qty": "1"},
        ],
        "output2": {"prsm_dpast": "50000000"},
    }
    with caplog.at_level("WARNING"):
        snapshot = parse_balance_response(response, _TS)

    assert snapshot.unknown_side_count == 1
    assert snapshot.same_direction_buy_count == 0
    assert snapshot.same_direction_sell_count == 0
    # 원본 값이 로그에 있어야 한다 — 그것이 이 경고의 유일한 목적이다.
    assert "'롱'" in caplog.text or "롱" in caplog.text
    assert "101S03" in caplog.text
    # 그리고 그 종목은 버려지지 않는다.
    assert len(snapshot.positions) == 1 and snapshot.positions[0].side == SIDE_UNKNOWN


def test_flattened_rows_are_not_unknown_they_are_simply_gone():
    """빈칸 + 잔고 0 = 당일 청산된 종목이다. 이것을 UNKNOWN으로 세면 **매일 오경보**가 난다."""
    response = {
        "output1": [{"shtn_pdno": "flattened", "sll_buy_dvsn_name": "", "cblc_qty": "0"}],
        "output2": {"prsm_dpast": "1000"},
    }
    snapshot = parse_balance_response(response, _TS)

    assert snapshot.unknown_side_count == 0
    assert snapshot.positions == ()


def test_parse_balance_response_extracts_the_position_detail_that_was_being_thrown_away():
    """015는 `output1`을 **방향별 개수 두 개로 요약해 버렸다.** 청산·그릭스·사후 재구성에
    필요한 종목별 상세(수량·평균단가·청산가능수량)가 응답에 이미 있는데 버려지고 있었다."""
    response = {
        "output1": [
            {
                "shtn_pdno": "201S03C325", "sll_buy_dvsn_name": "SLL", "sll_buy_dvsn_cd": "01",
                "cblc_qty": "2", "ccld_avg_unpr1": "3.55", "idx_clpr": "352.40",
                "evlu_pfls_amt": "-125000", "lqd_psbl_qty": "1",
            },
        ],
        "output2": {"prsm_dpast": "50000000"},
    }
    snapshot = parse_balance_response(response, _TS)
    (position,) = snapshot.positions

    assert position.symbol == "201S03C325"
    assert position.side == SIDE_SELL
    assert (position.qty, position.avg_price, position.current_price) == (2.0, 3.55, 352.40)
    assert position.eval_pnl == -125000.0
    # 청산가능수량은 보유수량과 다를 수 있다 — 강제청산은 이 값을 써야 한다.
    assert position.liquidatable_qty == 1.0
    # 원본은 통째로 보존된다(R8 — 컬럼은 문서 확인분이고 실측 확정은 8/18 뒤다).
    assert position.raw["ccld_avg_unpr1"] == "3.55"


def test_same_direction_count_puts_unknown_positions_on_the_blocking_side():
    """모르면 안전한 쪽 — UNKNOWN은 **후보 방향에 더한다**(진입을 막는 쪽).

    반대로 처리하면 KIS가 한글을 보내기 시작한 날 한도가 통째로 사라진다.
    """
    snapshot = BalanceSnapshot(
        timestamp=_TS, prsm_dpast=0.0, evlu_pfls_amt_smtl=0.0, trad_pfls_amt_smtl=0.0,
        dnca_cash=0.0, ord_psbl_cash=0.0, mgna_tota=0.0,
        same_direction_buy_count=1, same_direction_sell_count=0, unknown_side_count=2,
    )

    assert same_direction_positions(snapshot, "BUY") == 3  # 1 + 미인식 2
    assert same_direction_positions(snapshot, "SELL") == 2  # 0 + 미인식 2
    assert has_open_position_same_direction(snapshot, "BUY") is True
    assert has_open_position_same_direction(snapshot, "SELL") is True


def test_normal_days_are_byte_identical_to_the_old_behaviour():
    """`unknown_side_count == 0`이면 종전과 **한 비트도 다르지 않아야** 한다 —
    이 변경이 평시 판정을 움직이면 그것은 개시일에 섞이는 또 하나의 변수다."""
    snapshot = parse_balance_response(_RESPONSE, _TS)

    assert snapshot.unknown_side_count == 0
    assert same_direction_positions(snapshot, "BUY") == snapshot.same_direction_buy_count == 1
    assert same_direction_positions(snapshot, "SELL") == snapshot.same_direction_sell_count == 2
    state = build_account_state(snapshot, None, None, None, "BUY", {})
    assert state.same_direction_positions == 1


def test_has_open_position_same_direction_is_false_on_a_flat_account():
    """물타기 금지의 입력이다 — 포지션이 없으면 막을 것도 없다."""
    snapshot = parse_balance_response({"output2": {"prsm_dpast": "1000"}}, _TS)
    assert has_open_position_same_direction(snapshot, "BUY") is False
    assert has_open_position_same_direction(snapshot, "SELL") is False


def test_position_rows_carry_raw_payload_for_the_first_real_position_day():
    """8/18에 첫 포지션이 생기면 이 행 하나가 실측 범위표의 원재료가 된다(R8)."""
    snapshot = parse_balance_response(
        {
            "output1": [{"shtn_pdno": "101S03", "sll_buy_dvsn_name": "BUY", "cblc_qty": "1",
                         "ccld_avg_unpr1": "352.10", "lqd_psbl_qty": "1"}],
            "output2": {"prsm_dpast": "1000"},
        },
        _TS,
    )
    (row,) = position_rows(snapshot)

    assert row["timestamp"] == _TS
    assert row["symbol"] == "101S03"
    assert row["side"] == SIDE_BUY
    assert row["raw"]["ccld_avg_unpr1"] == "352.10"
    # `db.insert_position_snapshots()`가 기대하는 키가 전부 있는가.
    from mahdi.data.db import _POSITION_SNAPSHOT_COLUMNS

    assert set(row) == set(_POSITION_SNAPSHOT_COLUMNS)


def test_snapshot_to_row_carries_the_unknown_count_to_the_table():
    """0이 아닌 날은 그 행의 방향 카운트를 신뢰할 수 없다 — 그 사실이 DB에 남아야 한다."""
    snapshot = parse_balance_response(
        {
            "output1": [{"shtn_pdno": "x", "sll_buy_dvsn_name": "??", "cblc_qty": "1"}],
            "output2": {"prsm_dpast": "1000"},
        },
        _TS,
    )
    assert snapshot_to_row(snapshot)["unknown_side_count"] == 1
