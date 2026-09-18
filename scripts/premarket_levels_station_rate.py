"""맥점 = **정거장**(가격이 머무는 자리)으로 정의하고, 08:45 / 09:30 구조 레벨이 정거장을 맞히는지 잰다.

2026-09-06 [MW0601]. 선행(`premarket_levels_station_test.py`)은 되돌림·극값을 표적으로 했다. 여기서는
「되돌리는가」를 묻지 않고 **「그 자리에서 머무는가」**만 묻는다.

## 머무는 기준 — 데이터로 정한다

  밴드 b   : 1분봉 잡음의 배수. 검증창의 **1분봉 범위 중앙값(ATR14 단위)**을 재고 그 3배를 밴드로 쓴다.
             1분 잡음 안의 흔들림으로 밴드를 벗어나지 않게 하려는 최소 폭이다. (--band-mult로 바꿀 수 있다)
  체류 시간: 첫 터치 후 |가격 − 레벨| ≤ b 를 유지한 연속 분 수(stall). 그리고 세션 전체에서 ±b 안에 있던
             총 분 수(dwell)도 같이 본다.
  정거장    : stall이 **무작위 레벨의 stall 분포 75분위(T75)** 이상인 레벨. 「같은 거리에 아무 데나 그은 선보다
             눈에 띄게 오래 머문 자리」라는 뜻이다. T50·T90도 병기해 문턱 민감도를 보인다.

## 지표

  정거장률 = 터치된 레벨 중 stall ≥ T 인 비율 — 실제 vs 무작위(ATR 정규화 거리 셔플). 배율이 1이면 정보 없음.
  체류 분   = 세션 중 ±b 안 총 분 — 실제 vs 무작위.
  터치 안 된 레벨은 분모에서 뺀다(범위 밖 레벨은 정거장이 될 기회가 없다).

실행:
  python scripts/premarket_levels_station_rate.py --from 2026-01-05 --to 2026-09-04
  python scripts/premarket_levels_station_rate.py --from 2026-01-05 --to 2026-09-04 --at 09:30 --target after
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import premarket_levels_wfa as W

BIN = 0.5
RULE = dict(lookback=6, sources=frozenset(), spacing="nearest", gap_tilt=0, k=1.0)


def stall_and_dwell(bars, level: float, band: float):
    """(첫 터치 후 밴드 안 연속 분, 세션 전체 밴드 안 분). 터치 없으면 (None, dwell)."""
    dwell = sum(1 for x in bars if x[3] - band <= level <= x[2] + band)
    first = next((j for j, x in enumerate(bars) if x[3] <= level <= x[2]), None)
    if first is None:
        return None, dwell
    n = 0
    for x in bars[first:]:
        if x[3] - band <= level <= x[2] + band and abs(x[4] - level) <= band:
            n += 1
        else:
            break
    return n, dwell


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="맥점=정거장 정의의 검증")
    ap.add_argument("--db", type=Path, default=W.default_db())
    ap.add_argument("--from", dest="d_from", default="2026-01-05")
    ap.add_argument("--to", dest="d_to", default="2026-09-04")
    ap.add_argument("--at", default=None, help="09:30 재산출(기준가=현재가, 오프닝 레인지 후보 추가)")
    ap.add_argument("--target", choices=("day", "after"), default="day")
    ap.add_argument("--cut", default=None)
    ap.add_argument("--band-mult", type=float, default=3.0, help="밴드 = 1분봉 범위 중앙값 × 이 배수")
    ap.add_argument("--null", type=int, default=200)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    if not a.force:
        W.guard_intraday()
    sys.stdout.reconfigure(errors="replace")
    W.DAYS = W.load_days(a.db); W.DS = list(W.DAYS); ds = W.DS
    idxs = [i for i, d in enumerate(ds) if a.d_from <= d <= a.d_to and i >= 21 and not W.contaminated(i, RULE["lookback"])]
    cut = a.cut or a.at

    def bars_of(i):
        b = W.DAYS[ds[i]]
        return [x for x in b if x[0] > cut] if (a.target == "after" and cut) else b
    idxs = [i for i in idxs if len(bars_of(i)) >= 30]
    atr = {i: W.daily_atr(i) for i in idxs}

    # 밴드 — 1분봉 범위 중앙값(ATR 단위) × 배수
    bar_ranges = [(x[2] - x[3]) / atr[i] for i in idxs for x in W.DAYS[ds[i]]]
    noise = statistics.median(bar_ranges)
    band_atr = a.band_mult * noise
    print(f"표본 {ds[idxs[0]]}~{ds[idxs[-1]]} {len(idxs)}세션 · 1분봉 범위 중앙값 {noise:.3f} ATR → 밴드 = {a.band_mult:g}× = {band_atr:.3f} ATR "
          f"(1,050·ATR 42 기준 ≈ ±{band_atr*42:.1f}pt)")

    # 레벨
    if a.at:
        early = {i: [x for x in W.DAYS[ds[i]] if x[0] <= a.at] for i in idxs}
        idxs = [i for i in idxs if early[i]]
        refs = {i: early[i][-1][4] for i in idxs}
    else:
        refs = {i: W.DAYS[ds[i]][0][1] for i in idxs}
    real = []
    for i in idxs:
        merged = dict(W.candidates(i, RULE["lookback"], RULE["sources"]))
        if a.at:
            oh, ol = max(x[2] for x in early[i]), min(x[3] for x in early[i])
            for lv, tag in ((round(oh), "OR고"), (round(ol), "OR저")):
                key = next((k for k in merged if abs(k - lv) <= 1.5), None)
                if key is None:
                    merged[lv] = [tag]
                else:
                    merged[key] = merged[key] + [tag]
        gap = W.DAYS[ds[i]][0][1] - W.DAYS[ds[i - 1]][-1][4]
        u, dn = W.select_levels(merged, refs[i], RULE, atr[i], gap)
        real += [(i, k) for k in u + dn]
    print(f"레벨 = {'09:30 재산출(현재가 기준 + 오프닝 레인지)' if a.at else '08:45 시가 기준'} · 표적 구간 = {'그날 전체' if a.target == 'day' else cut + ' 이후'} · 레벨 {len(real)}개")

    # 격자 사전 채점
    grid = {}
    for i in idxs:
        bars = bars_of(i); hi = max(x[2] for x in bars); lo = min(x[3] for x in bars); px = refs[i]
        b = round((lo - 0.02 * px) / BIN) * BIN
        while b <= hi + 0.02 * px:
            grid[(i, round(b, 2))] = stall_and_dwell(bars, b, band_atr * atr[i])
            b = round(b + BIN, 2)

    def cell(i, k):
        return grid.get((i, round(round(k / BIN) * BIN, 2)))

    def measure(levels):
        stalls, dwells, touched = [], [], 0
        for i, k in levels:
            c = cell(i, k)
            if c is None:
                continue
            st, dw = c
            dwells.append(dw)
            if st is not None:
                touched += 1; stalls.append(st)
        return stalls, dwells, touched

    s_real, d_real, t_real = measure(real)

    # 무작위 — ATR 정규화 거리 셔플
    random.seed(23)
    dist = [(k - refs[i]) / atr[i] for i, k in real]
    null_stalls, null_dwell_means, null_rates = [], [], {q: [] for q in (50, 75, 90)}
    pooled = []
    for _ in range(a.null):
        random.shuffle(dist)
        fake = [(i, refs[i] + dist[j] * atr[i]) for j, (i, _) in enumerate(real)]
        s_n, d_n, _ = measure(fake)
        pooled += s_n
        null_stalls.append(s_n); null_dwell_means.append(statistics.mean(d_n) if d_n else 0)
    T = {q: float(np.quantile(pooled, q / 100)) for q in (50, 75, 90)}
    for s_n in null_stalls:
        for q in (50, 75, 90):
            null_rates[q].append(sum(1 for s in s_n if s >= T[q]) / len(s_n) if s_n else 0)

    def band_(xs):
        xs = sorted(xs); return xs[len(xs) // 2], xs[int(len(xs) * .05)], xs[int(len(xs) * .95)]

    print(f"\n터치된 레벨 {t_real}/{len(real)} ({t_real/len(real):.0%}) · 무작위 stall 분위: T50={T[50]:.0f}분 T75={T[75]:.0f}분 T90={T[90]:.0f}분")
    print(f"\n## 정거장률 — 터치된 레벨 중 첫 터치 후 밴드 안에 T분 이상 머문 비율")
    print(f"{'문턱':8} {'실제':>7} {'무작위 중앙':>10} {'90%구간':>15} {'배율':>6} {'판정':>6}")
    for q in (50, 75, 90):
        r = sum(1 for s in s_real if s >= T[q]) / len(s_real) if s_real else 0
        med, lo, hi = band_(null_rates[q])
        verdict = "우세" if r > hi else ("열세" if r < lo else "차이없음")
        print(f"T{q}≥{T[q]:.0f}분 {r:7.1%} {med:10.1%} {'%.1f~%.1f%%' % (lo*100, hi*100):>15} {r/med if med else 0:6.2f} {verdict:>6}")
    m = statistics.mean(d_real); med, lo, hi = band_(null_dwell_means)
    verdict = "우세" if m > hi else ("열세" if m < lo else "차이없음")
    print(f"\n## 체류 분 — 세션 중 밴드 ±{band_atr:.3f}ATR 안에 있던 분(레벨당 평균): 실제 {m:.1f} · 무작위 {med:.1f} [{lo:.1f}~{hi:.1f}] · {verdict}")
    print(f"   실제 stall 분포: 중앙 {statistics.median(s_real):.0f}분 · 상위25% {sorted(s_real)[3*len(s_real)//4]:.0f}분 · 0분(터치 즉시 이탈) {sum(1 for s in s_real if s == 0)/len(s_real):.0%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
