"""COCKPIT 데이터 소스 — DB 우선 조회, 실패/데이터 없음 시 합성 리플레이로 폴백.

폴백이 있는 이유: 대시보드는 실시간 수집 파이프라인이 아직 안 돌고 있어도(또는 장 종료 후에도)
독립 실행 가능해야 관측 인프라 검증에 쓸모가 있다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from mahdi.data import db
from mahdi.engines.regime import RegimeLabel
from mahdi.features.options_intel import OptionLeg, find_gamma_flip, gamma_walls as compute_gamma_walls

logger = logging.getLogger("mahdi.dashboard.data_source")

# 심볼 혼입 버그(2026-07-06) 시기에 쓰던 옛 고정 라벨 — 더 이상 아무도 안 쓰지만 남아있는
# 화석 데이터라 Flow Radar "가장 활발한 종목" 선정에서 제외한다.
_LEGACY_MIXED_SYMBOL = "KOSPI200_OPT"

# Flow Radar "가장 활발한 옵션" 선정 룩백 윈도 — 2026-07-06 위클리 북 추가 후 실측: 여러 위클리
# 종목이 같은 1분봉 timestamp로 동시에 찍혀("ORDER BY max(timestamp) DESC"에 동률), COCKPIT이
# 10초마다 리런될 때마다 임의로 다른 종목이 뽑혀 차트가 매번 완전히 다른 종목(다른 가격대)으로
# 바뀌어 보이는 문제가 실제로 발생함. "가장 최근 틱 1개"가 아니라 "최근 N분간 누적거래량"으로
# 기준을 바꿔, 단일 틱의 우연한 타이밍이 아니라 실제 상대적 활발함이 선정을 좌우하게 한다.
FLOW_RADAR_OPTION_LOOKBACK_MINUTES = 10


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
    # Flow Radar는 선물(기초자산)과 옵션(가장 활발한 종목) 두 계열을 따로 보여준다 — 선물은
    # WS 구독이 항상 켜져 있어 거의 매분 체결되므로, "가장 최근 활동"만으로 대표 종목을 뽑으면
    # 옵션이 영원히 안 뽑힌다(2026-07-06 사용자 지적으로 분리). VPIN은 종목 구분 없이 둘 다 계산된다.
    futures_flow_symbol: str | None
    timestamps: list[datetime]
    ofi_series: list[float]
    vpin_series: list[float]
    price_series: list[float]
    microprice_series: list[float]
    option_flow_symbol: str | None
    option_timestamps: list[datetime]
    option_ofi_series: list[float]
    option_vpin_series: list[float]
    option_price_series: list[float]
    option_microprice_series: list[float]
    foreign_net: float
    institution_net: float
    individual_net: float
    # Phase 1.5-④(2026-07-06 추가) — 먼슬리/위클리 북별 ATM±2 유동성 스냅샷(북당 최신 1건).
    # 각 dict 키: series, expiry, atm_spread_pct, depth, volume, days_to_expiry.
    expiry_liquidity: list[dict]


def load_snapshot(underlying: str = "KOSPI200") -> DashboardSnapshot:
    live = _load_from_db(underlying)
    return live if live is not None else _synthetic_snapshot()


def _load_from_db(underlying: str) -> DashboardSnapshot | None:
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
                # 리런 시각(datetime.now())이 아니라 스냅샷 자체의 최신 시각을 룩백 기준으로 쓴다 —
                # 장 마감 후 리플레이/재현 시나리오에서도 "최근 N분"이 항상 실제 데이터 시각 기준으로
                # 맞아야 하기 때문(datetime.now() 기준이면 지난 데이터를 볼 때 항상 윈도가 텅 빈다).
                as_of_ts = regime_row[0]

            spot = db.latest_underlying_spot(conn, underlying)
            if spot is None:
                return None

            chain_rows = db.latest_option_chain(conn, underlying)
            investor_flow = db.latest_investor_flow(conn, underlying)
            expiry_liquidity = db.latest_expiry_liquidity(conn, underlying)

            # 선물 계열: active_futures_symbol 레지스트리로 현재 구독 중인 선물 단축코드를
            # 명시적으로 조회한다(vpin 유무 같은 휴리스틱에 더 이상 의존하지 않음 — 2026-07-06,
            # 옵션에도 VPIN을 적용하면서 그 휴리스틱이 깨졌기 때문).
            futures_flow_symbol = db.get_active_futures_symbol(conn, underlying)

            with conn.cursor() as cur:
                futures_rows: list = []
                if futures_flow_symbol is not None:
                    cur.execute(
                        "SELECT timestamp, close, ofi, microprice, vpin FROM market_raw_1m "
                        "WHERE symbol=%s ORDER BY timestamp DESC LIMIT 60",
                        (futures_flow_symbol,),
                    )
                    futures_rows = cur.fetchall()

                # 옵션 계열: 선물이 WS 구독 덕에 거의 매분 체결돼 "가장 최근 활동"만으로 뽑으면
                # 옵션이 영원히 안 뽑힌다 — 선물 심볼과 화석 라벨을 명시적으로 제외한다. 단일 최근
                # 틱이 아니라 최근 룩백 윈도 누적거래량 기준으로 뽑아야 동률 타이밍에 매 리런마다
                # 종목이 바뀌는 문제(2026-07-06 위클리 도입 후 실측)가 없다. symbol ASC는 남은
                # 동률(거래량·시각 모두 같음)까지 결정론적으로 고정하기 위한 최종 타이브레이커.
                excluded_symbols = (_LEGACY_MIXED_SYMBOL, futures_flow_symbol or _LEGACY_MIXED_SYMBOL)
                lookback_cutoff = as_of_ts - timedelta(minutes=FLOW_RADAR_OPTION_LOOKBACK_MINUTES)
                cur.execute(
                    "SELECT symbol FROM market_raw_1m WHERE symbol NOT IN (%s, %s) AND timestamp >= %s "
                    "GROUP BY symbol ORDER BY sum(volume) DESC, max(timestamp) DESC, symbol ASC LIMIT 1",
                    (*excluded_symbols, lookback_cutoff),
                )
                option_row = cur.fetchone()
                option_flow_symbol = option_row[0] if option_row else None

                option_rows: list = []
                if option_flow_symbol is not None:
                    cur.execute(
                        "SELECT timestamp, close, ofi, microprice, vpin FROM market_raw_1m "
                        "WHERE symbol=%s ORDER BY timestamp DESC LIMIT 60",
                        (option_flow_symbol,),
                    )
                    option_rows = cur.fetchall()
    except Exception:
        # DB 미가동·마이그레이션 전·연결 실패 등 — 대시보드는 합성 데이터로 계속 동작해야 한다.
        # 2026-07-06: 예전엔 여기서 조용히 None만 반환해 왜 합성 폴백으로 빠졌는지 사후에 알 수
        # 없었다(오래 떠 있던 COCKPIT 프로세스가 옛 코드를 캐싱한 채 계속 폴백하던 사고) — 원인
        # 추적이 가능하도록 로그를 남긴다.
        logger.exception("실시간 데이터 조회 실패 — 합성 리플레이로 폴백")
        return None

    futures_rows = list(reversed(futures_rows))
    option_rows = list(reversed(option_rows))
    ts, regime_idx, prob_vector, higher_tf_idx, stability_flag = regime_row
    regime_prob = {RegimeLabel(i): float(p) for i, p in enumerate(prob_vector)}

    today = datetime.now().date()
    legs = [
        OptionLeg(
            strike=row["strike"],
            option_type=row["option_type"].lower(),
            oi=row["oi"],
            iv=row["iv"],
            t_years=max((row["expiry"] - today).days, 0) / 365.0,
            gamma=row["gamma"],
        )
        for row in chain_rows
        if row["expiry"] is not None
    ]

    by_strike: dict[float, float] = {}
    for row in chain_rows:
        by_strike[row["strike"]] = by_strike.get(row["strike"], 0.0) + row["gex"]
    chain = [ChainPoint(strike=s, gex=g) for s, g in sorted(by_strike.items())]

    if investor_flow is not None:
        foreign_net, institution_net, individual_net = investor_flow
    else:
        foreign_net, institution_net, individual_net = 0.0, 0.0, 0.0

    return DashboardSnapshot(
        as_of=ts,
        is_live=True,
        regime=RegimeLabel(regime_idx),
        regime_prob=regime_prob,
        higher_tf_regime=RegimeLabel(higher_tf_idx) if higher_tf_idx is not None else None,
        stability_flag=bool(stability_flag),
        spot=spot,
        chain=chain,
        gamma_flip=find_gamma_flip(legs, spot) if legs else None,
        gamma_walls=[strike for strike, _ in compute_gamma_walls(legs, spot)] if legs else [],
        futures_flow_symbol=futures_flow_symbol,
        timestamps=[row[0] for row in futures_rows],
        ofi_series=[float(row[2]) for row in futures_rows],
        vpin_series=[float(row[4]) if row[4] is not None else 0.0 for row in futures_rows],
        price_series=[float(row[1]) for row in futures_rows],
        microprice_series=[float(row[3]) for row in futures_rows],
        option_flow_symbol=option_flow_symbol,
        option_timestamps=[row[0] for row in option_rows],
        option_ofi_series=[float(row[2]) for row in option_rows],
        option_vpin_series=[float(row[4]) if row[4] is not None else 0.0 for row in option_rows],
        option_price_series=[float(row[1]) for row in option_rows],
        option_microprice_series=[float(row[3]) for row in option_rows],
        foreign_net=foreign_net,
        institution_net=institution_net,
        individual_net=individual_net,
        expiry_liquidity=expiry_liquidity,
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

    # 옵션 계열은 선물과 스케일이 다르다(체결가가 지수 포인트가 아니라 옵션 프리미엄) — 별도로 합성.
    option_price = 50.0 + np.cumsum(rng.normal(0, 0.2, n))
    option_ofi_series = rng.normal(0, 50, n).cumsum() * 0.05
    option_vpin_series = np.clip(0.3 + rng.normal(0, 0.15, n).cumsum() * 0.02, 0.05, 0.95)
    option_microprice_series = option_price + rng.normal(0, 0.05, n)

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
        futures_flow_symbol=None,
        timestamps=timestamps,
        ofi_series=list(ofi_series),
        vpin_series=list(vpin_series),
        price_series=list(spot),
        microprice_series=list(microprice_series),
        option_flow_symbol="SYNTH_OPT",
        option_timestamps=timestamps,
        option_ofi_series=list(option_ofi_series),
        option_vpin_series=list(option_vpin_series),
        option_price_series=list(option_price),
        option_microprice_series=list(option_microprice_series),
        foreign_net=float(rng.normal(0, 300)),
        institution_net=float(rng.normal(0, 200)),
        individual_net=float(rng.normal(0, 250)),
        expiry_liquidity=[
            {
                "series": "regular",
                "expiry": (now + timedelta(days=23)).date(),
                "atm_spread_pct": float(abs(rng.normal(0.04, 0.01))),
                "depth": float(abs(rng.normal(200, 40))),
                "volume": float(abs(rng.normal(500, 100))),
                "days_to_expiry": 23,
            },
            {
                "series": "weekly",
                "expiry": (now + timedelta(days=2)).date(),
                "atm_spread_pct": float(abs(rng.normal(0.09, 0.02))),
                "depth": float(abs(rng.normal(80, 20))),
                "volume": float(abs(rng.normal(150, 40))),
                "days_to_expiry": 2,
            },
        ],
    )
