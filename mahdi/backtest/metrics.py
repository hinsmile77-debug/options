"""Backtest — 성과 지표 (`cc_scorecard` 컬럼과 1:1 대응, v6 PART 14).

Phase 3 Champion-Challenger가 실거래 `trade_history`로 같은 지표를 계산할 때 이
모듈을 그대로 재사용할 수 있도록, 입력을 `net_pnl` 시퀀스 하나로 최소화했다.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SimulatedTrade:
    net_pnl: float


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    n_signals: int
    win_rate: float
    ev_after_cost: float  # net_pnl 평균
    max_dd: float  # 누적 손익 기준 최대 낙폭(0 또는 음수)
    cvar95: float  # 하위 5%(최소 1건) 손실의 평균(0 또는 음수)


def compute_metrics(trades: list[SimulatedTrade]) -> BacktestMetrics:
    """
    입력: SimulatedTrade(net_pnl) 리스트(체결된 거래만, 시간순).
    계산: win_rate=net_pnl>0 비율, ev_after_cost=net_pnl 평균, max_dd=누적합의 러닝
         최고점 대비 최대 하락폭, cvar95=하위 5%(최소 1건) 손실의 평균.
    실패 조건: trades가 비어 있으면 전부 0.0(신호 자체가 없었다는 뜻 — n_signals=0).
    """
    if not trades:
        return BacktestMetrics(n_signals=0, win_rate=0.0, ev_after_cost=0.0, max_dd=0.0, cvar95=0.0)

    pnls = [t.net_pnl for t in trades]
    n = len(pnls)
    win_rate = sum(1 for p in pnls if p > 0) / n
    ev_after_cost = sum(pnls) / n

    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        cumulative += p
        peak = max(peak, cumulative)
        max_dd = min(max_dd, cumulative - peak)

    sorted_pnls = sorted(pnls)
    tail_count = max(1, round(n * 0.05))
    cvar95 = sum(sorted_pnls[:tail_count]) / tail_count

    return BacktestMetrics(
        n_signals=n, win_rate=win_rate, ev_after_cost=ev_after_cost, max_dd=max_dd, cvar95=cvar95
    )
