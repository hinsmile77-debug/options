"""COCKPIT 데이터 소스 — DB 우선 조회, 실패/데이터 없음 시 합성 리플레이로 폴백.

폴백이 있는 이유: 대시보드는 실시간 수집 파이프라인이 아직 안 돌고 있어도(또는 장 종료 후에도)
독립 실행 가능해야 관측 인프라 검증에 쓸모가 있다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from mahdi.data import db
from mahdi.engines.regime import RegimeLabel


@dataclass(frozen=True, slots=True)
class ChainPoint:
    strike: float
    gex: float


@dataclass
class DashboardSnapshot:
    as_of: datetime
    is_live: bool  # DB에서 가져왔으면 True, 합성 폴백이면 False
    regime: RegimeLabel
    regime_prob: dict[RegimeLabel, float]
    higher_tf_regime: RegimeLabel | None
    stability_flag: bool
    spot: float
    chain: list[ChainPoint]
    gamma_flip: float | None
    gamma_walls: list[float]
    timestamps: list[datetime]
    ofi_series: list[float]
    vpin_series: list[float]
    price_series: list[float]
    microprice_series: list[float]
    foreign_net: float
    institution_net: float
    individual_net: float


def load_snapshot(symbol: str = "KOSPI200_OPT") -> DashboardSnapshot:
    live = _load_from_db(symbol)
    return live if live is not None else _synthetic_snapshot()


def _load_from_db(symbol: str) -> DashboardSnapshot | None:
    try:
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT timestamp, regime, prob_vector, higher_tf_regime, stability_flag "
                    "FROM regime_state ORDER BY timestamp DESC LIMIT 1"
                )
                regime_row = cur.fetchone()
                if regime_row is None:
                    return None

                cur.execute(
                    "SELECT timestamp, close, ofi, microprice FROM market_raw_1m "
                    "WHERE symbol=%s ORDER BY timestamp DESC LIMIT 60",
                    (symbol,),
                )
                market_rows = cur.fetchall()
                if not market_rows:
                    return None
    except Exception:
        # DB 미가동·마이그레이션 전·연결 실패 등 — 대시보드는 합성 데이터로 계속 동작해야 한다.
        return None

    market_rows = list(reversed(market_rows))
    ts, regime_idx, prob_vector, higher_tf_idx, stability_flag = regime_row
    regime_prob = {RegimeLabel(i): float(p) for i, p in enumerate(prob_vector)}

    return DashboardSnapshot(
        as_of=ts,
        is_live=True,
        regime=RegimeLabel(regime_idx),
        regime_prob=regime_prob,
        higher_tf_regime=RegimeLabel(higher_tf_idx) if higher_tf_idx is not None else None,
        stability_flag=bool(stability_flag),
        spot=float(market_rows[-1][1]),
        chain=[],  # option_analysis_1m 연동은 옵션 체인 수집기 가동 이후(Phase1 다음 단계)
        gamma_flip=None,
        gamma_walls=[],
        timestamps=[row[0] for row in market_rows],
        ofi_series=[float(row[2]) for row in market_rows],
        vpin_series=[0.0 for _ in market_rows],  # market_raw_1m.vpin 연동은 다음 단계
        price_series=[float(row[1]) for row in market_rows],
        microprice_series=[float(row[3]) for row in market_rows],
        foreign_net=0.0,
        institution_net=0.0,
        individual_net=0.0,
    )


def _synthetic_snapshot(seed: int | None = None) -> DashboardSnapshot:
    rng = np.random.default_rng(seed)
    now = datetime.now()
    n = 60
    timestamps = [now - timedelta(minutes=n - i) for i in range(n)]

    spot = 350.0 + np.cumsum(rng.normal(0, 0.15, n))
    ofi_series = rng.normal(0, 300, n).cumsum() * 0.05
    vpin_series = np.clip(0.3 + rng.normal(0, 0.15, n).cumsum() * 0.02, 0.05, 0.95)
    microprice_series = spot + rng.normal(0, 0.05, n)

    strikes = [340 + 2.5 * i for i in range(9)]
    chain = [ChainPoint(strike=s, gex=float(rng.normal(0, 1) * (1 if s < spot[-1] else -1) * 5e8)) for s in strikes]

    regime_prob = {r: 0.0 for r in RegimeLabel}
    dominant = rng.choice(list(RegimeLabel))
    remaining = [r for r in RegimeLabel if r != dominant]
    regime_prob[dominant] = 0.62
    leftover_share = 0.38 / len(remaining)
    for r in remaining:
        regime_prob[r] = leftover_share

    return DashboardSnapshot(
        as_of=now,
        is_live=False,
        regime=dominant,
        regime_prob=regime_prob,
        higher_tf_regime=None,
        stability_flag=regime_prob[dominant] >= 0.4,
        spot=float(spot[-1]),
        chain=chain,
        gamma_flip=float(spot[-1] - rng.uniform(-5, 5)),
        gamma_walls=[strikes[2], strikes[6]],
        timestamps=timestamps,
        ofi_series=list(ofi_series),
        vpin_series=list(vpin_series),
        price_series=list(spot),
        microprice_series=list(microprice_series),
        foreign_net=float(rng.normal(0, 300)),
        institution_net=float(rng.normal(0, 200)),
        individual_net=float(rng.normal(0, 250)),
    )
