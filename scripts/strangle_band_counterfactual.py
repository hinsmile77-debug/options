"""`small_strangle_buy` 델타 밴드 반사실 — **밴드를 어디에 두면 손익이 나은가**.

## 왜 이 스크립트가 있는가

2026-09-15 점검이 확정한 것: 진입조건을 통과한 판단(ENTER) 8,987건 중 64.3%가 종목을
못 골랐고, 그 사유의 92%가 `small_strangle_buy` 하나다(시도 3,161 · 성공 32 · **1.0%**).
원인은 규칙과 관측 창의 미스매치다 — 전략은 |델타| 0.20~0.30 콜·풋을 요구하는데 체인
수집 창은 ATM±2라 그 밴드에 닿는 레그가 거의 없다(09-08 최소 |델타| 0.340).

고치는 길이 둘인데 **둘의 손익이 다르면 그것이 선택의 근거**가 되어야 한다:

    A안  밴드를 창 안으로 당긴다 (|델타| 0.34~0.66) — 설정 한 줄, 수집 그대로
    B안  창을 넓혀 |델타| 0.20~0.30을 실제로 수집한다 — 구독 슬롯 증설
    C안  아무것도 안 하고 현행 유지 (straddle_accumulate = ATM 콜+풋)

## 실현손익은 0원이다 — 이것은 그 대체물이다

실주문이 0건이라 `trade_history`가 비어 있다. 여기서 내는 숫자는 **실현손익이 아니라
반사실 추정**이고, 세 가지를 실측에서 가져온다: 진입 프리미엄(`option_analysis_1m.price`),
분 단위 궤적(같은 표), 청산 판정(`exit_stack.evaluate_exit_stack()` — 라이브와 같은 함수).

## 무엇을 지어내지 않는가

- **레그를 합성하지 않는다.** B안의 델타 0.20~0.30 레그는 창 밖이라 대부분의 분에 DB에
  없다. BS로 가격을 만들어 넣으면 그 숫자가 곧 결론이 된다(08-05 스팟 괴리율에서 한 실수).
  대신 **스팟이 움직여 그 밴드가 우연히 창 안에 들어온 분만** 표본으로 쓰고, 표본 수를
  결과에 함께 싣는다. B안의 n이 작다는 것은 이 조사의 한계이자 그 자체로 하나의 발견이다.
- **청산 레이어를 지어내지 않는다.** 레이어 2·3(구조·플로우)은 그 시점의 VWAP/POC/플로우
  역전을 재구성해야 하는데 그 입력이 DB에 없다. 라이브 `_exit_market_state()`도 레이어 6만
  채우고 나머지를 비운다 — **같은 방식으로 비운다.** 즉 여기 손익은 하드스톱·타임스톱·
  15:10 강제청산 셋만 걸린 결과다.
- **슬리피지·수수료를 0으로 두지 않는다.** 둘 다 모르므로 **빼지 않은 총액**을 내고,
  그 사실을 열 이름(`gross`)이 말한다. 세 안을 같은 조건으로 비교하는 것이 목적이라
  공통 상수는 순위를 바꾸지 않는다.

## 사용법

    python scripts/strangle_band_counterfactual.py [--since 2026-08-18] [--until 2026-09-14]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mahdi.config.settings import get_strategy_params
from mahdi.data import db
from mahdi.engines.regime import RegimeLabel
from mahdi.execution.exit_stack import (
    MarketStructureState,
    PositionState,
    evaluate_exit_stack,
    exit_rules_key,
)
from mahdi.fusion import instrument_selection as isel
from mahdi.fusion.instrument_selection import (
    LiquidityThresholds,
    legs_from_chain_snapshot,
    select_instruments,
)

UNDERLYING = "KOSPI200"
# KOSPI200 옵션 거래승수(원/포인트). 계약 수는 1로 고정한다 — 사이징은 Risk가 정하는 것이고
# 여기서 비교하려는 것은 **같은 1계약을 어느 밴드로 골랐을 때** 손익이 어떤가다.
CONTRACT_MULTIPLIER = 250_000
STRATEGY = "small_strangle_buy"
# 15:10 이후는 강제청산 구간이라 신규 진입을 시뮬레이션하지 않는다(라이브도 14:50 컷오프).
ENTRY_CUTOFF = "14:50"

# 세 변형. A/B는 `_LegRule`의 델타 밴드만 바꾸고 나머지 규칙(콜·풋 양다리 BUY, 밴드 중앙
# 최근접, 유동성 필터)은 라이브 코드 그대로 쓴다.
# 값이 None인 변형은 밴드를 건드리지 않고 **그 전략을 그대로** 돌린다 — C안(현행 유지)의
# 기준선이다. 기준선 없이 A/B만 비교하면 "둘 중 나은 쪽"은 나와도 "고치는 것이 안 고치는
# 것보다 나은가"에는 답할 수 없다.
VARIANTS: dict[str, tuple[str, tuple[float, float] | None]] = {
    "B_현행밴드_0.20~0.30": ("small_strangle_buy", (0.20, 0.30)),
    "A_창안밴드_0.34~0.66": ("small_strangle_buy", (0.34, 0.66)),
    "C_현행유지_ATM스트래들": ("straddle_accumulate", None),
}


@dataclass(frozen=True, slots=True)
class Leg:
    expiry: date
    strike: float
    option_type: str


@dataclass
class Trade:
    variant: str
    entered_at: datetime
    legs: tuple[Leg, ...]
    entry_premium: float
    exit_at: datetime | None = None
    exit_premium: float | None = None
    exit_layer: str | None = None

    @property
    def pnl_pct(self) -> float | None:
        if self.exit_premium is None or not self.entry_premium:
            return None
        return (self.exit_premium - self.entry_premium) / self.entry_premium

    @property
    def pnl_won(self) -> float | None:
        if self.exit_premium is None:
            return None
        return (self.exit_premium - self.entry_premium) * CONTRACT_MULTIPLIER


def _patched_rules(strategy: str, delta_low: float, delta_high: float):
    """계산: 그 전략의 다리 규칙에서 델타 밴드만 바꾼 사본."""
    original = isel._STRATEGY_RULES[strategy]
    return tuple(replace(rule, delta_low=delta_low, delta_high=delta_high) for rule in original)


def _price_index(conn, since: date, until: date) -> dict[tuple[datetime, date, float, str], float]:
    """계산: (분, 만기, 행사가, 타입) -> 프리미엄. 궤적 추적을 매분 쿼리로 하지 않기 위한 색인."""
    index: dict[tuple[datetime, date, float, str], float] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT timestamp, expiry, strike, option_type, price FROM option_analysis_1m"
            " WHERE underlying=%s AND timestamp::date BETWEEN %s AND %s AND price IS NOT NULL",
            (UNDERLYING, since, until),
        )
        for ts, expiry, strike, option_type, price in cur.fetchall():
            index[(ts.replace(tzinfo=None), expiry, float(strike), str(option_type).upper())] = float(price)
    return index


def _regime_index(conn, since: date, until: date) -> dict[datetime, int]:
    index: dict[datetime, int] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT timestamp, regime FROM regime_state"
            " WHERE timestamp::date BETWEEN %s AND %s AND regime IS NOT NULL",
            (since, until),
        )
        for ts, regime in cur.fetchall():
            index[ts.replace(tzinfo=None)] = int(regime)
    return index


def _control_minutes(conn, since: date, until: date) -> list[tuple[datetime, int]]:
    """계산: 같은 기간의 REJECT 분(장중·컷오프 전). 방향은 판단이 남긴 부호를 그대로 쓴다."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT timestamp, coalesce((risk_gate_state->>'direction')::float, 1)"
            " FROM signal_decisions"
            " WHERE decision='REJECT' AND timestamp::date BETWEEN %s AND %s"
            "   AND timestamp::time >= '09:00' AND timestamp::time < %s"
            " ORDER BY timestamp",
            (since, until, ENTRY_CUTOFF),
        )
        return [
            (ts.replace(tzinfo=None), 1 if direction >= 0 else -1)
            for ts, direction in cur.fetchall()
        ]


def _entry_minutes(conn, since: date, until: date) -> list[tuple[datetime, int]]:
    """계산: `small_strangle_buy`가 열린 ENTER 분과 그 판단의 방향 부호.

    해석: **그 전략이 실행 가능했다면 진입했을 분**이 정확히 이 집합이다. 팔레트가 그 셀을
         안 연 분까지 세면 밴드 변경과 무관한 차이가 섞인다(db_metrics의 「주변 비율 대
         조건부 비율」과 같은 함정).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT timestamp, coalesce((risk_gate_state->>'direction')::float, 1)"
            " FROM signal_decisions"
            " WHERE decision='ENTER' AND timestamp::date BETWEEN %s AND %s"
            "   AND timestamp::time < %s"
            "   AND jsonb_typeof(risk_gate_state->'entry_strategies')='array'"
            "   AND risk_gate_state->'entry_strategies' @> %s::jsonb"
            " ORDER BY timestamp",
            (since, until, ENTRY_CUTOFF, json.dumps([STRATEGY])),
        )
        return [
            (ts.replace(tzinfo=None), 1 if direction >= 0 else -1)
            for ts, direction in cur.fetchall()
        ]


def _open_trade(
    legs,
    spot: float,
    variant: str,
    strategy: str,
    band: tuple[float, float] | None,
    now: datetime,
    direction: int,
) -> Trade | None:
    """계산: 그 분의 체인으로 라이브 선택기를 돌려, 밴드만 바꾼 후보가 나오는지 본다.

    `band`가 None이면 규칙을 건드리지 않는다 — 현행 그대로의 기준선.
    """
    saved = isel._STRATEGY_RULES[strategy]
    if band is not None:
        isel._STRATEGY_RULES[strategy] = _patched_rules(strategy, *band)
    try:
        result = select_instruments(
            [strategy],
            legs,
            spot,
            now.date(),
            direction=direction,
            thresholds=LiquidityThresholds.from_params(
                (get_strategy_params().get("instrument_selection") or {}).get("liquidity")
            ),
        )
    finally:
        isel._STRATEGY_RULES[strategy] = saved

    if not result.candidates:
        return None
    candidate = result.candidates[0]
    picked, premium = [], 0.0
    for leg in candidate.legs:
        if leg.price is None:
            return None  # 가격 없는 레그로 진입가를 지어내지 않는다
        picked.append(Leg(expiry=leg.expiry, strike=float(leg.strike), option_type=leg.option_type))
        premium += float(leg.price)
    if premium <= 0:
        return None
    return Trade(variant=variant, entered_at=now, legs=tuple(picked), entry_premium=premium)


def _session_start(moment: datetime) -> datetime:
    return moment.replace(hour=9, minute=0, second=0, microsecond=0)


def _close_trade(
    trade: Trade,
    prices: dict,
    regimes: dict,
    exit_rules_cfg: dict,
    hard_stop_pct: float = -0.02,
) -> Trade:
    """계산: 진입 다음 분부터 15:10까지 매분 `evaluate_exit_stack()`을 돌려 첫 트리거에 닫는다."""
    session_start = _session_start(trade.entered_at)
    forced_flat_at = trade.entered_at.replace(hour=15, minute=10, second=0, microsecond=0)
    moment = trade.entered_at + timedelta(minutes=1)
    last_seen_premium = trade.entry_premium
    last_seen_at = trade.entered_at

    while moment <= forced_flat_at:
        premium = 0.0
        complete = True
        for leg in trade.legs:
            price = prices.get((moment, leg.expiry, leg.strike, leg.option_type))
            if price is None:
                complete = False
                break
            premium += price
        if complete:
            last_seen_premium, last_seen_at = premium, moment
            regime = regimes.get(moment)
            # 레짐을 모르는 분은 그 분의 판정을 건너뛴다 — 모르는 레짐을 기본값으로 흘리면
            # 타임스톱이 조용히 사라진다(`exit_rules_key()` 주석이 경고한 그 경로).
            if regime is not None:
                position = PositionState(
                    symbol="|".join(f"{leg.option_type}{leg.strike:g}" for leg in trade.legs),
                    side="BUY",
                    entry_price=trade.entry_premium,
                    current_price=premium,
                    entry_time_minutes=(trade.entered_at - session_start).total_seconds() / 60.0,
                    now_minutes=(moment - session_start).total_seconds() / 60.0,
                    regime=exit_rules_key(
                        RegimeLabel(regime),
                        is_expiry_day=any(leg.expiry == moment.date() for leg in trade.legs),
                    ),
                )
                decision = evaluate_exit_stack(
                    position,
                    MarketStructureState(is_forced_flat_time=moment >= forced_flat_at),
                    None,  # 레이어 4는 EV 입력이 없어 평가하지 않는다(라이브와 동일)
                    exit_rules_cfg,
                    hard_stop_pct=hard_stop_pct,
                )
                if decision.action != "HOLD":
                    trade.exit_at = moment
                    trade.exit_premium = premium
                    trade.exit_layer = (
                        decision.triggered_layer.value if decision.triggered_layer else "unknown"
                    )
                    return trade
        moment += timedelta(minutes=1)

    # 15:10까지 아무 레이어도 안 걸린 경우 — 마지막으로 값을 본 분에 강제청산으로 닫는다.
    trade.exit_at = last_seen_at
    trade.exit_premium = last_seen_premium
    trade.exit_layer = "forced_flat" if last_seen_at > trade.entered_at else "no_trajectory"
    return trade


def _summarize(trades: list[Trade]) -> dict:
    closed = [t for t in trades if t.pnl_pct is not None and t.exit_layer != "no_trajectory"]
    if not closed:
        return {"n": 0}
    pnls = [t.pnl_pct for t in closed]
    wons = [t.pnl_won for t in closed]
    wins = [p for p in pnls if p > 0]
    layers: dict[str, int] = defaultdict(int)
    for t in closed:
        layers[t.exit_layer or "?"] += 1
    holds = [
        (t.exit_at - t.entered_at).total_seconds() / 60.0 for t in closed if t.exit_at is not None
    ]
    return {
        "n": len(closed),
        "avg_pnl_pct": sum(pnls) / len(pnls),
        "median_pnl_pct": sorted(pnls)[len(pnls) // 2],
        "total_gross_won": sum(wons),
        "avg_gross_won": sum(wons) / len(wons),
        "win_rate": len(wins) / len(pnls),
        "best_pct": max(pnls),
        "worst_pct": min(pnls),
        "avg_hold_min": sum(holds) / len(holds) if holds else 0.0,
        "layers": dict(sorted(layers.items(), key=lambda kv: -kv[1])),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default="2026-08-18", help="실행 배선 이후 기본값")
    parser.add_argument("--until", default="2026-09-14")
    # 손절 민감도 — `exit_rules`의 레짐별 `stop`은 **진입가 대비 비율**이고(설정 주석),
    # 옵션 매수의 진입가는 프리미엄이다. 프리미엄 -0.8%가 기초자산 -0.8%와 같은 뜻이 아니므로
    # 이 축을 흔들어 본다. `--ignore-regime-stop`은 레짐 손절을 끄고 절대 한도만 남긴다.
    parser.add_argument("--hard-stop", type=float, default=-0.02)
    parser.add_argument("--ignore-regime-stop", action="store_true")
    # 대조군 — **마흐디가 진입하지 말라고 한 분**에 같은 방식으로 진입한다. 진입 신호에
    # 알파가 있다면 ENTER 분의 손익이 REJECT 분보다 나아야 한다. 두 값이 같으면 그 신호는
    # 손익을 가르지 못한다는 뜻이고, 그때 밴드를 고치는 것은 지는 판을 더 많이 까는 일이다.
    parser.add_argument("--control", action="store_true", help="REJECT 분을 대상으로 돌린다")
    args = parser.parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d").date()
    until = datetime.strptime(args.until, "%Y-%m-%d").date()

    exit_rules_cfg = get_strategy_params().get("exit_rules") or {}
    if args.ignore_regime_stop:
        # `stop` 키만 뺀다 — `time_stop`은 남긴다. 둘을 같이 끄면 어느 쪽이 손익을 움직였는지
        # 갈리지 않는다.
        exit_rules_cfg = {
            key: {k: v for k, v in (row or {}).items() if k != "stop"}
            for key, row in exit_rules_cfg.items()
        }

    with db.get_connection() as conn:
        if args.control:
            minutes = _control_minutes(conn, since, until)
            print(f"대조군: {len(minutes)}건 ({since} ~ {until}, REJECT 분)")
        else:
            minutes = _entry_minutes(conn, since, until)
            print(f"대상 진입 분: {len(minutes)}건 ({since} ~ {until}, {STRATEGY}가 열린 ENTER 분)")
        prices = _price_index(conn, since, until)
        regimes = _regime_index(conn, since, until)
        print(f"가격 색인 {len(prices):,}행 · 레짐 색인 {len(regimes):,}행")

        results: dict[str, list[Trade]] = {name: [] for name in VARIANTS}
        for i, (moment, direction) in enumerate(minutes, 1):
            if i % 500 == 0:
                print(f"  ... {i}/{len(minutes)}")
            # 체인·스팟은 분당 한 번만 읽는다 — 변형마다 다시 읽으면 같은 스냅샷을 세 번
            # 쿼리하게 되고, 그 사이 무엇도 달라지지 않는다.
            legs = legs_from_chain_snapshot(db.option_chain_as_of(conn, UNDERLYING, moment))
            spot = db.latest_underlying_spot(conn, UNDERLYING, as_of=moment)
            if not legs or spot is None:
                continue
            for name, (strategy, band) in VARIANTS.items():
                trade = _open_trade(legs, spot, name, strategy, band, moment, direction)
                if trade is not None:
                    results[name].append(
                        _close_trade(trade, prices, regimes, exit_rules_cfg, args.hard_stop)
                    )

    print("\n" + "=" * 78)
    stop_note = "레짐손절 OFF" if args.ignore_regime_stop else "레짐손절 ON"
    print(f"반사실 손익 — 1계약 기준, 승수 {CONTRACT_MULTIPLIER:,}원, 수수료·슬리피지 미차감(gross)")
    print(f"청산 설정 — 절대한도 {args.hard_stop:.1%} · {stop_note} · 타임스톱 ON · 15:10 강제청산 ON")
    print("=" * 78)
    attempts = len(minutes)
    for name in VARIANTS:
        summary = _summarize(results[name])
        print(f"\n[{name}]")
        if not summary["n"]:
            print(f"  진입 성립 0건 / 시도 {attempts}건 — 그 밴드로는 표본이 없다")
            continue
        print(f"  진입 성립     {summary['n']}건 / 시도 {attempts}건 ({summary['n']/attempts:.1%})")
        print(f"  평균 손익률   {summary['avg_pnl_pct']:+.2%}   중앙값 {summary['median_pnl_pct']:+.2%}")
        print(f"  승률          {summary['win_rate']:.1%}")
        print(f"  총 손익(gross) {summary['total_gross_won']:+,.0f}원")
        print(f"  건당 평균      {summary['avg_gross_won']:+,.0f}원")
        print(f"  최고/최저     {summary['best_pct']:+.2%} / {summary['worst_pct']:+.2%}")
        print(f"  평균 보유     {summary['avg_hold_min']:.1f}분")
        print(f"  청산 레이어   {summary['layers']}")


if __name__ == "__main__":
    main()
