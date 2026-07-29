"""Execution Engine — 계좌 손익/포지션 상태 추적기.

`RiskEngine.evaluate_entry()`/`evaluate_ongoing()`이 필요로 하는 `AccountState`를
`get_balance()`(선물옵션 잔고현황, CTFO6118R/VTFO6118R) 응답으로부터 만든다. 필드 매핑은
2026-07-28 7차 세션에서 라이브 모의계좌로 직접 호출해 확인한 실측 결과 그대로다
([[DECISION_LOG]] 참고) — 추측으로 채운 필드가 없다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from mahdi.risk.limits import AccountState


@dataclass(frozen=True, slots=True)
class BalanceSnapshot:
    timestamp: datetime
    prsm_dpast: float  # 추정예탁자산 — 일간/주간 손익, 드로우다운의 기준값
    evlu_pfls_amt_smtl: float  # 평가손익금액합계
    trad_pfls_amt_smtl: float  # 매매손익금액합계
    dnca_cash: float
    ord_psbl_cash: float
    mgna_tota: float
    same_direction_buy_count: int
    same_direction_sell_count: int


def _to_float(value) -> float:
    return float(value) if value not in (None, "") else 0.0


def parse_balance_response(response: dict, timestamp: datetime) -> BalanceSnapshot:
    """
    입력: `KISRestClient.get_balance()` 원본 응답(`output1` 배열 + `output2` dict), 조회 시각.
    계산: `output2`의 문자열 숫자 필드를 float으로 변환하고, `output1`의
         `sll_buy_dvsn_name`("BUY"/"SLL")을 세어 방향별 포지션 수를 만든다("당일 잔고를
         청산하여 잔고가 없는 경우 빈칸으로 출력"되는 값은 어느 쪽으로도 세지 않는다).
    실패 조건: `output2`가 없으면(비정상 응답 — 예: 요청 파라미터 오류로 인한 실패 응답)
              ValueError. `output1`이 없으면 빈 배열로 취급(포지션 없음).
    """
    output2 = response.get("output2")
    if output2 is None:
        raise ValueError("get_balance() 응답에 output2가 없습니다")

    positions = response.get("output1") or []
    buy_count = sum(1 for p in positions if p.get("sll_buy_dvsn_name") == "BUY")
    sell_count = sum(1 for p in positions if p.get("sll_buy_dvsn_name") == "SLL")

    return BalanceSnapshot(
        timestamp=timestamp,
        prsm_dpast=_to_float(output2.get("prsm_dpast")),
        evlu_pfls_amt_smtl=_to_float(output2.get("evlu_pfls_amt_smtl")),
        trad_pfls_amt_smtl=_to_float(output2.get("trad_pfls_amt_smtl")),
        dnca_cash=_to_float(output2.get("dnca_cash")),
        ord_psbl_cash=_to_float(output2.get("ord_psbl_cash")),
        mgna_tota=_to_float(output2.get("mgna_tota")),
        same_direction_buy_count=buy_count,
        same_direction_sell_count=sell_count,
    )


def snapshot_to_row(snapshot: BalanceSnapshot) -> dict:
    """`db.insert_account_balance_snapshot()`에 바로 넘길 수 있는 dict로 펼친다."""
    return {
        "timestamp": snapshot.timestamp,
        "prsm_dpast": snapshot.prsm_dpast,
        "evlu_pfls_amt_smtl": snapshot.evlu_pfls_amt_smtl,
        "trad_pfls_amt_smtl": snapshot.trad_pfls_amt_smtl,
        "dnca_cash": snapshot.dnca_cash,
        "ord_psbl_cash": snapshot.ord_psbl_cash,
        "mgna_tota": snapshot.mgna_tota,
        "same_direction_buy_count": snapshot.same_direction_buy_count,
        "same_direction_sell_count": snapshot.same_direction_sell_count,
    }


def _pct_change(current: float, baseline: float) -> float:
    if baseline == 0:
        return 0.0
    return (current - baseline) / baseline


def build_account_state(
    latest: BalanceSnapshot,
    start_of_day: BalanceSnapshot | None,
    start_of_week: BalanceSnapshot | None,
    peak_prsm_dpast: float | None,
    candidate_side: str,
    daily_trades_by_strategy: dict[str, int],
    pending_trade_loss_pct: float = 0.0,
) -> AccountState:
    """
    입력: 최신 스냅샷, 오늘/이번주 시작 시점 기준 스냅샷(`db.account_balance_snapshot_before()`
         — 없으면 baseline 미확정), 역대 최고 `prsm_dpast`(`db.max_account_balance_ever()` —
         없으면 드로우다운 미확정), 진입하려는 방향("BUY"/"SELL"), 전략별 오늘 거래 횟수,
         진입하려는 트레이드의 예상 최대손실률.
    계산: `daily_pnl_pct`/`weekly_pnl_pct` = (latest.prsm_dpast − baseline.prsm_dpast) /
         baseline.prsm_dpast, `drawdown_pct` = (latest.prsm_dpast − peak) / peak,
         `same_direction_positions`는 `candidate_side`에 맞는 buy/sell 카운트를 그대로 쓴다
         (매수 후보면 기존 매수 포지션 수, 매도 후보면 기존 매도 포지션 수 — v6 §12.2
         "동일 방향 동시 포지션" 한도가 후보와 같은 방향의 기존 포지션만 센다는 의미와 일치).
    해석: baseline/peak이 없으면(운영 첫 날 등) 0.0으로 폴백 — "손익 없음"이 아니라 "아직 비교할
         과거가 없다"는 뜻이지만, 두 상태를 리스크 게이트 관점에서 구분할 필요는 없다(어느
         쪽이든 한도 위반은 아니므로).
    실패 조건: 없음 — 전부 안전한 기본값으로 흡수.
    """
    daily_baseline = start_of_day.prsm_dpast if start_of_day is not None else 0.0
    weekly_baseline = start_of_week.prsm_dpast if start_of_week is not None else 0.0
    peak = peak_prsm_dpast if peak_prsm_dpast is not None else 0.0

    same_direction = (
        latest.same_direction_buy_count if candidate_side.upper() == "BUY" else latest.same_direction_sell_count
    )

    return AccountState(
        daily_pnl_pct=_pct_change(latest.prsm_dpast, daily_baseline) if start_of_day is not None else 0.0,
        weekly_pnl_pct=_pct_change(latest.prsm_dpast, weekly_baseline) if start_of_week is not None else 0.0,
        drawdown_pct=_pct_change(latest.prsm_dpast, peak) if peak_prsm_dpast is not None else 0.0,
        same_direction_positions=same_direction,
        daily_trades_by_strategy=daily_trades_by_strategy,
        pending_trade_loss_pct=pending_trade_loss_pct,
    )
