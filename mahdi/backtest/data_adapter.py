"""Backtest — 실데이터 DB 어댑터 (v6 PART 21 "백테스트 엔진").

`mahdi/backtest/engine.py`가 요구하는 `BacktestStep` 시퀀스를 실제 DB(과거 1분봉·옵션체인·
투자자수급·레짐)로부터 만든다. `main.py`의 `_build_signal_inputs()`(라이브 배선, 2026-07-28
2차)와 같은 피처 조합 로직을 쓰되, "지금 기준 최신"이 아니라 각 봉 시각 기준(as-of)으로
과거를 리플레이한다는 점만 다르다.

**알려진 단순화(둘 다 실행 경로 검증용이지 실제 과거 손익 재현이 아님)**:
1. 레짐은 라벨만 복원한다 — `regime_state` 테이블에 과거 `prob_vector`가 저장돼 있지만
   `db.latest_regime_before()`는 라벨(int)만 반환하므로, 여기서는 원-핫 확률벡터로 근사한다.
2. `AccountState`/`MarketConditions`는 항상 중립 기본값이다 — 과거 일별 손익/드로우다운을
   재구성하는 인프라(계좌 상태 추적기)가 아직 없다(main.py 라이브 배선과 같은 제약,
   [[DECISION_LOG]] 참고). 따라서 이 어댑터로 만든 백테스트는 Risk Engine의 한도/Circuit
   Breaker가 사실상 항상 통과하는 조건에서 실행된다 — Signal Fusion/Execution 로직 자체의
   실행 경로 검증에는 충분하지만, "그 시절 실제로 이 계좌로 거래했다면"을 재현하지는 않는다.
"""

from __future__ import annotations

from datetime import datetime

from mahdi.backtest.engine import BacktestStep
from mahdi.backtest.simulated_broker import Bar
from mahdi.data import db
from mahdi.engines.regime import RegimeLabel, RegimeState
from mahdi.execution.exit_stack import MarketStructureState, exit_rules_key
from mahdi.features.options_intel import calculate_gex, find_gamma_flip, legs_from_chain_rows
from mahdi.fusion.engine import MetaLabelContext
from mahdi.fusion.instrument_selection import is_expiry_day, legs_from_chain_snapshot
from mahdi.fusion.signal_layer import SignalInputs
from mahdi.risk.circuit_breaker import MarketConditions
from mahdi.risk.limits import AccountState

_NEUTRAL_ACCOUNT_STATE = AccountState(
    daily_pnl_pct=0.0,
    weekly_pnl_pct=0.0,
    drawdown_pct=0.0,
    same_direction_positions=0,
    daily_trades_by_strategy={},
    pending_trade_loss_pct=0.0,
)
_NEUTRAL_MARKET_CONDITIONS = MarketConditions(
    vpin=0.0, vix=0.0, usdkrw_daily_change_pct=0.0, data_quality_ok=True, model_drift_detected=False
)


def _regime_state_from_label(label: int | None) -> RegimeState | None:
    """계산: 라벨(int)만으로 원-핫 확률벡터를 근사 복원한다. 실패 조건: label이 None이면 None."""
    if label is None:
        return None
    prob_vector = [0.0] * len(RegimeLabel)
    prob_vector[label] = 1.0
    return RegimeState(regime=RegimeLabel(label), prob_vector=tuple(prob_vector), stability_flag=True)


def load_backtest_steps_from_db(
    conn,
    underlying: str,
    futures_symbol: str,
    start: datetime,
    end: datetime,
) -> list[BacktestStep]:
    """
    입력: DB 커넥션, underlying 라벨, 선물 심볼, 조회 구간(start 이상 end 미만).
    계산: `db.market_bars_between()`으로 봉을 뽑고, 각 봉 시각을 as-of 기준으로
         `db.option_chain_as_of()`/`db.investor_flow_as_of()`/`db.latest_regime_before()`를
         조회해 `SignalInputs`를 구성한다(GEX/Gamma Flip은 `legs_from_chain_rows`+
         `calculate_gex`/`find_gamma_flip` 재사용). OFI/큐임밸런스는 라이브과 동일하게 항상
         None(집계 파이프라인 없음).
    해석: 반환된 `BacktestStep` 시퀀스를 그대로 `BacktestEngine.run()`에 넘기면 된다.
    실패 조건: 구간에 봉이 없으면 빈 목록.
    """
    bars = db.market_bars_between(conn, futures_symbol, start, end)

    steps: list[BacktestStep] = []
    for row in bars:
        as_of = row["timestamp"]
        chain_rows = db.option_chain_as_of(conn, underlying, as_of)
        flow = db.investor_flow_as_of(conn, underlying, as_of)
        regime_label = db.latest_regime_before(conn, as_of)

        gex = gamma_flip = None
        if chain_rows:
            legs = legs_from_chain_rows(chain_rows, today=as_of.date())
            if legs:
                gex = calculate_gex(legs, row["close"])
                gamma_flip = find_gamma_flip(legs, row["close"])

        signal_inputs = SignalInputs(
            regime_state=_regime_state_from_label(regime_label),
            gex=gex,
            gamma_flip=gamma_flip,
            spot=row["close"],
            ofi=None,
            queue_imbalance=None,
            foreign_net_flow=flow[0] if flow is not None else None,
        )

        steps.append(
            BacktestStep(
                bar=Bar(
                    # 유닉스 epoch 분 단위 — 여러 날에 걸친 리플레이에서도 경과시간(time_stop)이
                    # 단조증가하도록 세션 상대값 대신 절대값을 쓴다. `engine.py`의
                    # `_minutes_to_time()`은 이 값을 시각(time-of-day)으로도 변환하는데, 이렇게
                    # 큰 절대값은 23:59로 클램프된다 — 다만 이 어댑터가 만드는 EntryContext는
                    # negative_gex_expansion을 항상 기본값(False)으로 두므로(urgency 판단 자체를
                    # 안 함) 그 클램프가 실제 동작에 영향을 주지 않는다. Urgency Mode를 백테스트에
                    # 연결하는 다음 증분에서는 이 시간-표현 이원화를 다시 검토해야 한다.
                    timestamp_minutes=as_of.timestamp() / 60.0,
                    open=row["open"],
                    high=row["high"],
                    low=row["low"],
                    close=row["close"],
                ),
                signal_inputs=signal_inputs,
                account_state=_NEUTRAL_ACCOUNT_STATE,
                market_conditions=_NEUTRAL_MARKET_CONDITIONS,
                meta_context=MetaLabelContext(),
                market_structure=MarketStructureState(),
                # 2026-08-17 — **그 봉 시점에 실제로 관측된 레짐**을 청산 규칙 키로 넘긴다.
                #
                # 이 인자를 비우면 `BacktestStep`이 `_DEFAULT_REGIME_FOR_EXIT_RULES`
                # ("TREND_STRONG")로 떨어지고, 실측 레짐 이력에서 그 계열은 **0분**이다.
                # 즉 백테스트가 한 번도 일어난 적 없는 레짐의 청산 파라미터(time_stop 120분,
                # 레짐 손절 없음)로 손익 분포를 재고 있었다 — 실제로 71.7%를 차지하는
                # VOL_COMPRESSION은 `exit_rules`에 행이 아예 없어 타임스톱이 안 걸린다.
                #
                # **그 사실을 여기서 숨기지 않는 것이 요점이다**: 이제 백테스트도 미정의 레짐을
                # 만나면 `resolve_exit_params()`의 경고를 그대로 맞는다. 값을 정하는 것은
                # 사람의 일이고, 그 전까지 백테스트 결과는 그 구간에 시간 방어선이 없었음을
                # 반영해야 한다(낙관적으로 보이던 종전 결과가 그 반대였다).
                #
                # 만기 당일 여부는 §11.5 선택기와 **같은 함수**로 판정한다 — 두 곳이 따로
                # 판정하면 한쪽은 0DTE 파라미터로 청산하면서 다른 쪽은 일반 후보를 만드는
                # 상태가 되고, 어느 쪽이 옳은지 알 방법이 없어진다.
                exit_rules_regime=(
                    None
                    if regime_label is None
                    else exit_rules_key(
                        RegimeLabel(regime_label),
                        is_expiry_day=is_expiry_day(
                            legs_from_chain_snapshot(chain_rows), as_of.date()
                        ),
                    )
                ),
            )
        )
    return steps
